from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import PlatformAdapterType
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import asyncio
import aiohttp
import json
import struct
import traceback

@register("minecraft_monitor", "YourName", "Minecraft服务器监控插件", "2.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        self.enable_auto_monitor = self.config.get("enable_auto_monitor", False)
        self.monitor_count_only = self.config.get("monitor_count_only", False)

        # 解析多服务器配置
        self.servers = self._parse_servers_config()

        if not self.servers:
            logger.error("配置不完整(server_ip/port/target_group)，监控无法启动")
            self.enable_auto_monitor = False
        else:
            for s in self.servers:
                logger.info(f"MC监控已加载 | 服务器: {s['ip']}:{s['port']} | 群: {s['group']} | 间隔: {s['interval']}s")

        if self.enable_auto_monitor:
            asyncio.create_task(self._delayed_auto_start())

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------

    def _split_config(self, value):
        """将配置值按分号分割，返回去空白后的列表；None / 空字符串返回空列表"""
        if not value:
            return []
        return [v.strip() for v in str(value).split(";") if v.strip()]

    def _parse_servers_config(self):
        """根据配置构建服务器信息列表，支持多服务器（分号分隔）"""
        ips = self._split_config(self.config.get("server_ip", ""))
        if not ips:
            return []

        n = len(ips)

        def expand(raw_list, default, label):
            """将 raw_list 扩展到长度 n；若只有 1 个元素则广播；数量不匹配时使用 default"""
            if len(raw_list) == n:
                return raw_list
            if len(raw_list) == 1:
                return raw_list * n
            if raw_list:
                logger.warning(
                    f"{label} 的数量({len(raw_list)})与服务器IP数量({n})不匹配，"
                    f"将对所有服务器使用默认值: {default}"
                )
            return [str(default)] * n

        raw_ports = self._split_config(self.config.get("server_port", "25565"))
        if not raw_ports:
            raw_ports = ["25565"]
        ports = expand(raw_ports, "25565", "server_port")

        raw_names = self._split_config(self.config.get("server_name", "Minecraft服务器"))
        if not raw_names:
            raw_names = ["Minecraft服务器"]
        names = expand(raw_names, "Minecraft服务器", "server_name")

        raw_groups = self._split_config(self.config.get("target_group", ""))
        groups = expand(raw_groups, "", "target_group")

        raw_intervals = self._split_config(self.config.get("check_interval", "10"))
        if not raw_intervals:
            raw_intervals = ["10"]
        intervals = expand(raw_intervals, "10", "check_interval")

        servers = []
        for i in range(n):
            group = groups[i]
            if group and not group.isdigit():
                logger.error(f"target_group '{group}' 不是有效数字，跳过服务器 {ips[i]}")
                continue

            port_str = ports[i]
            port = int(port_str) if port_str.isdigit() else 25565

            interval_str = intervals[i]
            interval = int(interval_str) if interval_str.isdigit() else 10

            servers.append({
                'ip': ips[i],
                'port': port,
                'name': names[i],
                'group': group,
                'interval': interval,
                # 运行时状态
                'last_player_count': None,
                'last_player_list': set(),
                'task': None,
            })

        return servers

    # ------------------------------------------------------------------
    # 自动启动
    # ------------------------------------------------------------------

    async def _delayed_auto_start(self):
        await asyncio.sleep(5)
        for s in self.servers:
            if not s['task'] or s['task'].done():
                s['task'] = asyncio.create_task(self.monitor_task(s))
        logger.info(f"🚀 自动启动服务器监控任务（共 {len(self.servers)} 个服务器）")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    async def get_hitokoto(self):
        """获取一言"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://v1.hitokoto.cn/?encode=text", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    return await resp.text() if resp.status == 200 else None
        except Exception as e:
            logger.debug(f"获取一言失败: {e}")
            return None

    def _parse_players(self, players_data):
        """解析玩家列表，返回名字列表"""
        if not players_data:
            return []

        # 标准格式：列表包含字典 [{"name": "player1"}, ...]
        if isinstance(players_data, list):
            return [p.get("name", str(p)) if isinstance(p, dict) else str(p) for p in players_data]

        return []

    def _pack_varint(self, val):
        """将整数打包为VarInt格式（Minecraft协议）"""
        total = b""
        if val < 0:
            val = (1 << 32) + val
        while True:
            byte = val & 0x7F
            val >>= 7
            if val != 0:
                byte |= 0x80
            total += bytes([byte])
            if val == 0:
                break
        return total

    async def _read_varint(self, reader):
        """从流中读取VarInt格式的整数（Minecraft协议）"""
        val = 0
        shift = 0
        bytes_read = 0
        max_bytes = 5  # VarInt最多5字节
        while True:
            byte = await reader.read(1)
            if len(byte) == 0:
                raise Exception("Connection closed")
            b = byte[0]
            val |= (b & 0x7F) << shift
            bytes_read += 1
            if bytes_read > max_bytes:
                raise Exception("VarInt too big")
            if (b & 0x80) == 0:
                break
            shift += 7
        return val

    async def _ping_server(self, host, port):
        """使用Minecraft Server List Ping协议直接查询服务器"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10.0
            )
        except asyncio.TimeoutError:
            logger.warning(f"服务器Ping失败: {host}:{port} - 连接超时(10秒)")
            return None
        except ConnectionRefusedError:
            logger.warning(f"服务器Ping失败: {host}:{port} - 连接被拒绝(服务器可能未运行)")
            return None
        except Exception as e:
            logger.warning(f"服务器Ping失败: {host}:{port} - {type(e).__name__}: {e}")
            return None

        try:
            # 发送握手包
            host_bytes = host.encode("utf-8")
            handshake = (
                b"\x00"
                + self._pack_varint(-1)  # Protocol version: -1 for status
                + self._pack_varint(len(host_bytes))
                + host_bytes
                + struct.pack(">H", int(port))
                + self._pack_varint(1)  # Next state: 1 for status
            )
            packet = self._pack_varint(len(handshake)) + handshake
            writer.write(packet)

            # 发送状态请求包
            request = b"\x00"
            packet = self._pack_varint(len(request)) + request
            writer.write(packet)
            await writer.drain()

            # 读取响应
            async def read_response():
                length = await self._read_varint(reader)
                packet_id = await self._read_varint(reader)

                if packet_id == 0:
                    json_len = await self._read_varint(reader)
                    data = await reader.readexactly(json_len)
                    decoded_data = data.decode("utf-8")
                    logger.debug(f"MC Server response: {decoded_data}")
                    return json.loads(decoded_data)
                return None

            return await asyncio.wait_for(read_response(), timeout=10.0)

        except asyncio.TimeoutError:
            logger.warning(f"服务器Ping失败: {host}:{port} - 读取响应超时(10秒)")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"服务器Ping失败: {host}:{port} - JSON解析错误: {e}")
            return None
        except Exception as e:
            logger.warning(f"服务器Ping失败: {host}:{port} - {type(e).__name__}: {e}")
            return None
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError, asyncio.CancelledError):
                pass

    async def _fetch_server_data(self, server):
        """获取Minecraft服务器数据（使用直接Socket连接）"""
        host = server['ip']
        port = server['port']
        name = server['name']

        if not host or not port:
            return None

        try:
            data = await self._ping_server(host, int(port))
            logger.debug(f"MC Server raw data: {data}")

            if not data:
                return {
                    'status': 'offline',
                    'name': name,
                    'version': '未知',
                    'online': 0,
                    'max': 0,
                    'player_names': [],
                    'motd': ''
                }

            # 检查是否为正常的服务器信息
            if "version" in data and "players" in data:
                version = data.get("version", {}).get("name", "未知版本")
                players_info = data.get("players", {})
                online_players = players_info.get("online", 0)
                max_players = players_info.get("max", 0)
                player_sample = players_info.get("sample", [])

                # 提取MOTD
                motd_data = data.get("description", "")
                if isinstance(motd_data, dict):
                    motd = motd_data.get("text", "")
                else:
                    motd = str(motd_data) if motd_data else ""

                # 提取玩家名
                player_names = self._parse_players(player_sample)

                return {
                    'status': 'online',
                    'name': name,
                    'version': version,
                    'online': online_players,
                    'max': max_players,
                    'player_names': player_names,
                    'motd': motd
                }

            # 可能是启动中或其他状态
            return {
                'status': 'starting',
                'name': name,
                'version': '启动中',
                'online': 0,
                'max': 0,
                'player_names': [],
                'motd': str(data)
            }

        except Exception as e:
            logger.error(f"获取服务器信息出错 ({host}:{port}): {e}")
            return None

    def _format_msg(self, data):
        if not data:
            return "❌ 无法连接到服务器"

        # Add status emoji based on server status
        if data.get('status') == 'online':
            status_emoji = "🟢"
        elif data.get('status') == 'starting':
            status_emoji = "🟡"
        else:
            status_emoji = "🔴"
        msg = [f"{status_emoji} 服务器: {data['name']}"]

        if data.get('motd'):
            msg.append(f"📝 MOTD: {data['motd']}")

        msg.append(f"🎮 版本: {data['version']}")
        msg.append(f"👥 在线玩家: {data['online']}")

        # Only show player list section if there are players online
        if data.get('player_names') and data['online'] > 0:
            names = data['player_names']
            p_str = ", ".join(names[:10])
            if len(names) > 10:
                p_str += f" 等{len(names)}人"
            msg.append(f"📋 玩家列表: {p_str}")

        return "\n".join(msg)

    async def monitor_task(self, server):
        """定时监控核心逻辑（针对单个服务器）"""
        while True:
            try:
                data = await self._fetch_server_data(server)

                if data and data['status'] == 'online':
                    curr_online = data['online']
                    curr_players = set(data['player_names'])

                    # 首次运行初始化
                    if server['last_player_count'] is None:
                        server['last_player_count'] = curr_online
                        server['last_player_list'] = curr_players
                        logger.info(f"[{server['name']}] 监控初始化完成，当前在线: {curr_online}人")
                    else:
                        # 检测变化
                        changes = []

                        if self.monitor_count_only:
                            # 仅监控人数变化模式
                            if curr_online != server['last_player_count']:
                                diff = curr_online - server['last_player_count']
                                symbol = "📈" if diff > 0 else "📉"
                                changes.append(f"{symbol} 在线人数变化: {diff:+d} (当前 {curr_online}人)")
                        else:
                            last_players = server['last_player_list']

                            joined = curr_players - last_players
                            left = last_players - curr_players

                            if joined:
                                changes.append(f"📈 {', '.join(joined)} 加入了服务器")
                            if left:
                                changes.append(f"📉 {', '.join(left)} 离开了服务器")

                            # 如果只有数量变化但获取不到具体名单（部分服务端特性）
                            if not joined and not left and curr_online != server['last_player_count']:
                                diff = curr_online - server['last_player_count']
                                symbol = "📈" if diff > 0 else "📉"
                                changes.append(f"{symbol} 在线人数变化: {diff:+d} (当前 {curr_online}人)")

                        if changes:
                            logger.info(f"[{server['name']}] 🔔 检测到变化: {changes}")
                            # 构建完整消息
                            notify_msg = "🔔 状态变动:\n" + "\n".join(changes)
                            notify_msg += f"\n\n{self._format_msg(data)}"

                            hito = await self.get_hitokoto()
                            if hito:
                                notify_msg += f"\n\n💬 {hito}"

                            logger.info(f"[{server['name']}] 准备发送变动通知消息，长度: {len(notify_msg)} 字符")
                            await self.send_group_msg(notify_msg, server['group'])

                        # Log status after each query cycle
                        logger.info(f"[{server['name']}] 自动查询完成 - 在线: {curr_online}人, 状态: 正常")

                        # 更新缓存
                        server['last_player_count'] = curr_online
                        server['last_player_list'] = curr_players

                elif data is None:
                    # 获取失败时暂不处理，避免断网刷屏，仅日志
                    logger.debug(f"[{server['name']}] 获取服务器数据失败")
                else:
                    # Handle other server statuses
                    if data.get('status') == 'starting':
                        logger.info(f"[{server['name']}] 自动查询完成 - 服务器状态: 启动中")
                    else:
                        logger.info(f"[{server['name']}] 自动查询完成 - 服务器状态: {data.get('status', '未知')}")

                await asyncio.sleep(server['interval'])

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{server['name']}] 监控循环异常: {e}")
                await asyncio.sleep(5)

    async def send_group_msg(self, text, group):
        """
        主动发送消息到指定 QQ 群
        :param text: 要发送的消息内容
        :param group: 目标群号（字符串或整数）
        """
        if not group:
            logger.warning("消息发送失败: target_group 未配置")
            return
        try:
            # 从插件上下文中获取 AIOCQHTTP (OneBot) 平台适配器
            platform = self.context.get_platform(PlatformAdapterType.AIOCQHTTP)

            if not platform:
                logger.error("未找到 AIOCQHTTP 平台适配器，无法发送消息")
                return

            # 获取底层的 API 客户端
            client = platform.get_client()

            if not client:
                logger.error("无法获取 AIOCQHTTP 客户端，无法发送消息")
                return

            # 调用标准的 OneBot v11 API: send_group_msg
            logger.info(f"正在发送消息到群 {group}")
            await client.api.call_action('send_group_msg', **{
                'group_id': int(group),
                'message': text
            })
            logger.info(f"✅ 消息已发送到群 {group}")
        except Exception as e:
            logger.error(f"❌ 消息发送失败到群 {group}: {type(e).__name__}: {e}")
            logger.error(f"详细错误信息:\n{traceback.format_exc()}")

    # --- 指令区域 ---

    @filter.command("start_server_monitor")
    async def cmd_start(self, event: AstrMessageEvent):
        if not self.servers:
            yield event.plain_result("❌ 没有已配置的服务器，请检查配置")
            return
        started = []
        already_running = []
        for s in self.servers:
            if s['task'] and not s['task'].done():
                already_running.append(s['name'])
            else:
                s['task'] = asyncio.create_task(self.monitor_task(s))
                started.append(s['name'])
        parts = []
        if started:
            parts.append(f"✅ 已启动监控: {', '.join(started)}")
        if already_running:
            parts.append(f"⚠️ 已在运行中: {', '.join(already_running)}")
        yield event.plain_result("\n".join(parts))

    @filter.command("stop_server_monitor")
    async def cmd_stop(self, event: AstrMessageEvent):
        for s in self.servers:
            if s['task']:
                s['task'].cancel()
                try:
                    await s['task']
                except asyncio.CancelledError:
                    pass
                s['task'] = None
        yield event.plain_result("🛑 所有服务器监控已停止")

    @filter.command("查询")
    async def cmd_query(self, event: AstrMessageEvent):
        if not self.servers:
            yield event.plain_result("❌ 没有已配置的服务器，请检查配置")
            return
        hito = await self.get_hitokoto()
        for i, s in enumerate(self.servers):
            data = await self._fetch_server_data(s)
            msg = self._format_msg(data)
            if hito and i == len(self.servers) - 1:
                msg += f"\n\n💬 {hito}"
            yield event.plain_result(msg)

    @filter.command("reset_monitor")
    async def cmd_reset(self, event: AstrMessageEvent):
        for s in self.servers:
            s['last_player_count'] = None
            s['last_player_list'] = set()
        yield event.plain_result("🔄 缓存已重置，下次检测将视为首次")

    @filter.command("set_group")
    async def cmd_setgroup(self, event: AstrMessageEvent, group_id: str):
        if group_id.isdigit():
            for s in self.servers:
                s['group'] = group_id
            yield event.plain_result(f"✅ 所有服务器的目标群已设为: {group_id}")
        else:
            yield event.plain_result("❌ 群号必须为纯数字")

    async def terminate(self):
        for s in self.servers:
            if s['task']:
                s['task'].cancel()
                try:
                    await s['task']
                except asyncio.CancelledError:
                    pass
