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
        self.task = None
        
        # 配置处理
        self.target_group = self.config.get("target_group")
        if self.target_group and not str(self.target_group).isdigit():
            logger.error(f"target_group '{self.target_group}' 不是有效数字")
            self.target_group = None

        self.server_name = self.config.get("server_name", "Minecraft服务器")
        self.server_ip = self.config.get("server_ip")
        self.server_port = self.config.get("server_port")
        self.check_interval = int(self.config.get("check_interval", 10))
        self.enable_auto_monitor = self.config.get("enable_auto_monitor", False)
        
        # 缓存数据
        self.last_player_count = None
        self.last_player_list = []
        
        if not self.target_group or not self.server_ip or not self.server_port:
            logger.error("配置不完整(target_group/ip/port)，监控无法启动")
            self.enable_auto_monitor = False
        else:
            logger.info(f"MC监控已加载 | 服务器: {self.server_ip}:{self.server_port}")
        
        if self.enable_auto_monitor:
            asyncio.create_task(self._delayed_auto_start())

    async def _delayed_auto_start(self):
        await asyncio.sleep(5)
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.monitor_task())
            logger.info("🚀 自动启动服务器监控任务")

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

    async def _fetch_server_data(self):
        """获取Minecraft服务器数据（使用直接Socket连接）"""
        if not self.server_ip or not self.server_port:
            return None
        
        try:
            data = await self._ping_server(self.server_ip, int(self.server_port))
            logger.debug(f"MC Server raw data: {data}")

            if not data:
                return {
                    'status': 'offline',
                    'name': self.server_name,
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
                    'name': self.server_name,
                    'version': version,
                    'online': online_players,
                    'max': max_players,
                    'player_names': player_names,
                    'motd': motd
                }
            
            # 可能是启动中或其他状态
            return {
                'status': 'starting',
                'name': self.server_name,
                'version': '启动中',
                'online': 0,
                'max': 0,
                'player_names': [],
                'motd': str(data)
            }

        except Exception as e:
            logger.error(f"获取服务器信息出错: {e}")
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

    async def monitor_task(self):
        """定时监控核心逻辑"""
        while True:
            try:
                data = await self._fetch_server_data()
                
                if data and data['status'] == 'online':
                    curr_online = data['online']
                    curr_players = set(data['player_names'])
                    
                    # 首次运行初始化
                    if self.last_player_count is None:
                        self.last_player_count = curr_online
                        self.last_player_list = curr_players
                        logger.info(f"监控初始化完成，当前在线: {curr_online}人")
                    else:
                        # 检测变化
                        changes = []
                        last_players = self.last_player_list
                        
                        joined = curr_players - last_players
                        left = last_players - curr_players
                        
                        if joined:
                            changes.append(f"📈 {', '.join(joined)} 加入了服务器")
                        if left:
                            changes.append(f"📉 {', '.join(left)} 离开了服务器")
                            
                        # 如果只有数量变化但获取不到具体名单（部分服务端特性）
                        if not joined and not left and curr_online != self.last_player_count:
                            diff = curr_online - self.last_player_count
                            symbol = "📈" if diff > 0 else "📉"
                            changes.append(f"{symbol} 在线人数变化: {diff:+d} (当前 {curr_online}人)")

                        if changes:
                            logger.info(f"🔔 检测到变化: {changes}")
                            # 构建完整消息
                            notify_msg = "🔔 状态变动:\n" + "\n".join(changes)
                            notify_msg += f"\n\n{self._format_msg(data)}"
                            
                            hito = await self.get_hitokoto()
                            if hito: notify_msg += f"\n\n💬 {hito}"
                            
                            logger.info(f"准备发送变动通知消息，长度: {len(notify_msg)} 字符")
                            await self.send_group_msg(notify_msg)
                        
                        # Log status after each query cycle
                        logger.info(f"自动查询完成 - 在线: {curr_online}人, 状态: 正常")
                        
                        # 更新缓存
                        self.last_player_count = curr_online
                        self.last_player_list = curr_players
                
                elif data is None:
                    # 获取失败时暂不处理，避免断网刷屏，仅日志
                    logger.debug("获取服务器数据失败")
                else:
                    # Handle other server statuses
                    if data.get('status') == 'starting':
                        logger.info(f"自动查询完成 - 服务器状态: 启动中")
                    else:
                        logger.info(f"自动查询完成 - 服务器状态: {data.get('status', '未知')}")
                
                await asyncio.sleep(self.check_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"监控循环异常: {e}")
                await asyncio.sleep(5)

    async def send_group_msg(self, text):
        """
        主动发送消息到指定 QQ 群
        :param text: 要发送的消息内容
        """
        if not self.target_group:
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
            logger.info(f"正在发送消息到群 {self.target_group}")
            await client.api.call_action('send_group_msg', **{
                'group_id': int(self.target_group),
                'message': text
            })
            logger.info(f"✅ 消息已发送到群 {self.target_group}")
        except Exception as e:
            logger.error(f"❌ 消息发送失败到群 {self.target_group}: {type(e).__name__}: {e}")
            logger.error(f"详细错误信息:\n{traceback.format_exc()}")

    # --- 指令区域 ---

    @filter.command("start_server_monitor")
    async def cmd_start(self, event: AstrMessageEvent):
        if self.task and not self.task.done():
            yield event.plain_result("⚠️ 监控已在运行中")
        else:
            self.task = asyncio.create_task(self.monitor_task())
            yield event.plain_result(f"✅ 监控已启动 (间隔{self.check_interval}s)")

    @filter.command("stop_server_monitor")
    async def cmd_stop(self, event: AstrMessageEvent):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        yield event.plain_result("🛑 监控已停止")

    @filter.command("查询")
    async def cmd_query(self, event: AstrMessageEvent):
        data = await self._fetch_server_data()
        msg = self._format_msg(data)
        hito = await self.get_hitokoto()
        if hito: msg += f"\n\n💬 {hito}"
        yield event.plain_result(msg)

    @filter.command("reset_monitor")
    async def cmd_reset(self, event: AstrMessageEvent):
        self.last_player_count = None
        self.last_player_list = []
        yield event.plain_result("🔄 缓存已重置，下次检测将视为首次")

    @filter.command("set_group")
    async def cmd_setgroup(self, event: AstrMessageEvent, group_id: str):
        if group_id.isdigit():
            self.target_group = group_id
            yield event.plain_result(f"✅ 目标群已设为: {group_id}")
        else:
            yield event.plain_result("❌ 群号必须为纯数字")

    async def terminate(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


