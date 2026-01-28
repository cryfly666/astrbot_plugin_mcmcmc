from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.platform import MessageType
from astrbot.api.event.filter import PlatformAdapterType
import asyncio
import aiohttp
import json

@register("minecraft_monitor", "YourName", "Minecraft服务器监控插件，定时获取服务器状态", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.task = None  # 用于存储定时任务
        
        # 从配置获取参数，不再使用具体的默认值
        target_group_raw = self.config.get("target_group")
        self.target_group = None
        
        # 验证target_group是否为有效数字
        if target_group_raw is not None:
            target_group_str = str(target_group_raw)
            if target_group_str.isdigit():
                self.target_group = target_group_str
            else:
                logger.error(f"配置中的 target_group '{target_group_raw}' 不是有效的数字，已忽略。")
        
        self.server_name = self.config.get("server_name", "Minecraft服务器")
        self.server_ip = self.config.get("server_ip")
        self.server_port = self.config.get("server_port")
        self.server_type = self.config.get("server_type", "be")  # 添加服务器类型配置，默认为基岩版(be)
        self.check_interval = self.config.get("check_interval", 10)
        self.enable_auto_monitor = self.config.get("enable_auto_monitor", False)
        
        # 状态缓存，用于检测变化
        self.last_player_count = None  # 上次的玩家数量，None表示未初始化
        self.last_player_list = []     # 上次的玩家列表
        self.last_status = None        # 上次的服务器状态
        
        # 检查必要的配置是否完整
        if not self.target_group or not self.server_ip or not self.server_port:
            logger.error("Minecraft监控插件配置不完整，缺少 target_group、server_ip 或 server_port，自动监控功能将不会启动。")
            logger.error("请在配置文件中设置以下参数: target_group, server_ip, server_port")
            self.enable_auto_monitor = False
        else:
            logger.info(f"Minecraft监控插件已加载 - 目标群: {self.target_group}, 服务器: {self.server_ip}:{self.server_port}, 类型: {self.server_type}")
        
        # 如果启用了自动监控且配置完整，延迟启动任务
        if self.enable_auto_monitor:
            asyncio.create_task(self._delayed_auto_start())
    
    async def _delayed_auto_start(self):
        """延迟自动启动监控任务"""
        await asyncio.sleep(5)  # 等待5秒让插件完全初始化
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.direct_hello_task())
            logger.info("🚀 自动启动服务器监控任务")
    
    async def get_hitokoto(self):
        """获取一言句子"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://v1.hitokoto.cn/?encode=text", timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        text = await response.text()
                        return text.strip()
                    else:
                        logger.warning(f"获取一言失败: HTTP {response.status}")
                        return None
        except aiohttp.ClientError as e:
            logger.warning(f"获取一言网络请求失败: {e}")
            return None
        except asyncio.TimeoutError:
            logger.warning("获取一言请求超时")
            return None
        except Exception as e:
            logger.warning(f"获取一言时发生未知错误: {e}")
            return None

    def _extract_player_names(self, player_sample):
        """
        从player_sample中提取玩家名称列表
        兼容：列表 / 字典列表 / 字符串
        """
        if not player_sample:
            return []

        # 情况1：API 返回字符串 "A, B, C"
        if isinstance(player_sample, str):
            return [name.strip() for name in player_sample.split(",") if name.strip()]

        # 情况2：API 返回列表
        if isinstance(player_sample, list):
            names = []
            for p in player_sample:
                if isinstance(p, dict):
                    # 基岩版API可能使用不同的字段名
                    name = p.get("name") or p.get("username") or p.get("name_clean") or p.get("playername") or p.get("xuid")
                    if name:
                        names.append(str(name))
                else:
                    names.append(str(p))
            return names

        # 其他情况（不认识的格式）
        return []

    async def _fetch_server_data(self):
        """
        获取Minecraft服务器原始数据
        
        Returns:
            dict: 包含服务器信息的字典，失败时返回None
        """
        # 检查配置完整性
        if not self.server_ip or not self.server_port:
            logger.error("服务器IP或端口未配置")
            return None
        
        try:
            url = f"https://motd.minebbs.com/api/status?ip={self.server_ip}&port={self.server_port}&stype={self.server_type}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        try:
                            data = await response.json()
                            logger.info(f"API返回数据: {data}")  # 调试日志
                        except json.JSONDecodeError:
                            logger.error(f"API响应JSON解析失败: {await response.text()}")
                            return None
                        
                        # 根据实际API格式提取服务器信息
                        server_status = data.get('status', '未知')
                        
                        # 处理版本信息
                        version = data.get('version', '未知版本')
                        # 基岩版API可能返回版本为null或空
                        if not version or version == 'null':
                            version = '未知版本'
                        
                        # 处理玩家信息 - 基岩版API返回格式可能与Java版不同
                        players_info = data.get('players', {})
                        
                        # 基岩版API可能直接返回online和max，也可能是字符串
                        if isinstance(players_info, dict):
                            # 获取玩家数量，确保转换为整数
                            online_players_raw = players_info.get('online', 0)
                            max_players_raw = players_info.get('max', 0)
                            
                            # 转换为整数，处理可能的字符串或null值
                            try:
                                online_players = int(online_players_raw) if online_players_raw else 0
                            except (ValueError, TypeError):
                                online_players = 0
                            
                            try:
                                max_players = int(max_players_raw) if max_players_raw else 0
                            except (ValueError, TypeError):
                                max_players = 0
                            
                            # 基岩版API的玩家列表可能在不同的字段中
                            player_sample = None
                            
                            # 尝试多个可能的字段名来获取玩家列表
                            possible_player_fields = ['sample', 'list', 'players', 'player_sample', 'online_players']
                            for field in possible_player_fields:
                                if field in players_info and players_info[field]:
                                    player_sample = players_info[field]
                                    logger.info(f"从字段 '{field}' 获取到玩家列表: {player_sample}")
                                    break
                            
                            # 如果上述字段都没有，检查是否有其他玩家信息格式
                            if not player_sample and online_players > 0:
                                # 基岩版API可能直接将玩家信息放在players_info中
                                if len(players_info) > 2:  # 除了online和max之外还有其他字段
                                    # 可能是玩家信息以其他格式存储
                                    for key, value in players_info.items():
                                        if key not in ['online', 'max'] and isinstance(value, (list, str)):
                                            player_sample = value
                                            logger.info(f"从字段 '{key}' 获取到玩家信息: {player_sample}")
                                            break
                        else:
                            online_players = 0
                            max_players = 0
                            player_sample = []
                        
                        # 对于基岩版，服务器名称可能来自不同字段
                        server_name = data.get('hostname', self.server_name)
                        if not server_name or server_name == 'null':
                            server_name = self.server_name
                        
                        # 处理motd信息
                        motd = data.get('motd', {})
                        if isinstance(motd, dict):
                            motd_text = motd.get('clean', [])
                            if isinstance(motd_text, list):
                                motd_text = ' '.join([str(item) for item in motd_text])
                        else:
                            motd_text = str(motd) if motd else ''
                        
                        return {
                            'status': server_status,
                            'name': server_name,
                            'version': version,
                            'online': online_players,
                            'max': max_players,
                            'players': player_sample if player_sample is not None else [],
                            'motd': motd_text
                        }
                    else:
                        logger.warning(f"获取服务器信息失败 (状态码: {response.status})")
                        return None
                        
        except aiohttp.ClientError as e:
            logger.error(f"网络请求失败: {e}")
            return None
        except asyncio.TimeoutError:
            logger.warning("请求超时")
            return None
        except Exception as e:
            logger.error(f"获取服务器信息时发生未知错误: {e}")
            return None
    
    def _format_server_info(self, server_data):
        """
        将服务器原始数据格式化为可读消息
        
        Args:
            server_data: 从_fetch_server_data获取的数据字典
            
        Returns:
            str: 格式化后的消息，失败时返回错误信息
        """
        if server_data is None:
            return "❌ 获取服务器数据失败"
        
        server_status = server_data['status']
        server_name = server_data['name']
        version = server_data['version']
        online_players = server_data['online']
        max_players = server_data['max']
        player_sample = server_data['players']
        motd = server_data.get('motd', '')
        
        # 构建消息
        status_emoji = "🟢" if server_status == "online" else "🔴"
        message = f"{status_emoji} 服务器: {server_name}\n"
        
        # 添加MOTD信息
        if motd and motd != 'null' and motd != '[]':
            message += f"📝 MOTD: {motd}\n"
            
        message += f"🎮 版本: {version}\n"
        message += f"👥 在线玩家: {online_players}/{max_players}"
        
        # 处理玩家列表
        if online_players > 0:
            player_names = self._extract_player_names(player_sample)
            if player_names:
                display_names = player_names[:10]
                message += f"\n📋 玩家列表: {', '.join(display_names)}"
                if len(player_names) > 10:
                    message += f" (+{len(player_names) - 10}人)"
            else:
                # 如果有玩家在线但无法获取列表，显示提示信息
                message += f"\n📋 当前有 {online_players} 名玩家在线"
        else:
            message += "\n📋 当前无玩家在线"
        
        # 添加服务器类型标识
        server_type_display = "基岩版" if self.server_type == "be" else "Java版"
        message += f"\n🔧 服务器类型: {server_type_display}"
        
        return message

    async def get_minecraft_server_info(self, format_message=True):
        """
        获取Minecraft服务器信息
        
        Args:
            format_message: 是否格式化为消息字符串，False时返回原始数据字典
            
        Returns:
            str或dict: 格式化的消息或原始数据字典
        """
        server_data = await self._fetch_server_data()
        
        if not format_message:
            return server_data
        
        return self._format_server_info(server_data)
    
    def check_server_changes(self, server_data):
        """检查服务器状态是否有变化，返回是否需要发送消息和变化描述"""
        if server_data is None:
            return False, "获取服务器数据失败"
        
        current_online = server_data['online']
        current_players = server_data['players']
        current_status = server_data['status']
        
        # 使用统一的玩家名称提取方法
        current_player_names = self._extract_player_names(current_players)
        
        # 检查是否是首次检查（使用 None 判断）
        if self.last_player_count is None:
            # 首次检查，更新缓存但不发送消息（除非有玩家在线）
            self.last_player_count = current_online
            self.last_player_list = current_player_names.copy()
            self.last_status = current_status
            
            if current_online > 0:
                return True, "服务器监控已启动，当前有玩家在线"
            else:
                return True, "服务器监控已启动"
        
        # 检查变化
        changes = []
        
        # 不推送服务器上下线变化，只推送玩家变化
        
        # 检查玩家数量变化
        player_diff = current_online - self.last_player_count
        if player_diff > 0:
            # 有玩家加入
            new_players = set(current_player_names) - set(self.last_player_list)
            if new_players:
                changes.append(f"📈 {', '.join(new_players)} 加入了服务器 (+{player_diff})")
            else:
                changes.append(f"📈 有 {player_diff} 名玩家加入了服务器")
        elif player_diff < 0:
            # 有玩家离开
            left_players = set(self.last_player_list) - set(current_player_names)
            if left_players:
                changes.append(f"📉 {', '.join(left_players)} 离开了服务器 ({player_diff})")
            else:
                changes.append(f"📉 有 {abs(player_diff)} 名玩家离开了服务器")
        
        # 更新缓存
        self.last_player_count = current_online
        self.last_player_list = current_player_names.copy()
        self.last_status = current_status
        
        # 如果有变化，返回True和变化描述
        if changes:
            return True, "\n".join(changes)
        else:
            return False, "无变化"
    
    async def initialize(self):
        """插件初始化方法"""
        logger.info("Minecraft服务器监控插件已加载，使用 /start_server_monitor 启动定时任务")
    
    async def notify_subscribers(self, message: str):
        """发送通知到目标群组（抽象的通知函数）"""
        if not self.target_group:
            logger.error("❌ 目标群号未配置，无法发送通知")
            return False
        
        try:
            # 验证群号格式（双重保险）
            if not self.target_group.isdigit():
                logger.error(f"❌ 无效的群号格式: {self.target_group}")
                return False
            
            # 获取AIOCQHTTP客户端并发送
            platform = self.context.get_platform(PlatformAdapterType.AIOCQHTTP)
            
            if not platform or not hasattr(platform, 'get_client'):
                logger.error("❌ 无法获取AIOCQHTTP客户端")
                return False
                
            client = platform.get_client()
            
            result = await client.api.call_action('send_group_msg', **{
                'group_id': int(self.target_group),
                'message': message
            })
            
            if result and result.get('message_id'):
                logger.info(f"✅ 已发送通知到群 {self.target_group}")
                return True
            else:
                logger.warning(f"❌ 发送失败: {result}")
                return False
        except ValueError as e:
            logger.error(f"❌ 群号转换失败: {self.target_group}, 错误: {e}")
            return False
        except Exception as e:
            logger.error(f"发送通知时出错: {e}")
            return False
    
    async def direct_hello_task(self):
        """定时获取并检测Minecraft服务器变化"""
        while True:
            try:
                # 等待配置的检查间隔
                await asyncio.sleep(self.check_interval)
                
                # 仅获取一次服务器原始数据
                server_data = await self._fetch_server_data()
                
                if server_data is None:
                    logger.warning("❌ 获取服务器数据失败，跳过本次检查")
                    continue
                
                # 检查是否有变化
                should_send, change_message = self.check_server_changes(server_data)
                
                if should_send:
                    # 有变化，发送消息
                    # 先发送变化提醒
                    change_notification = f"🔔 服务器状态变化：\n{change_message}"
                    
                    # 使用已获取的数据格式化完整状态（避免第二次网络请求）
                    full_status = self._format_server_info(server_data)
                    
                    # 获取一言句子
                    hitokoto = await self.get_hitokoto()
                    
                    # 构建最终消息
                    final_message = f"{change_notification}\n\n📊 当前状态：\n{full_status}"
                    if hitokoto:
                        final_message += f"\n\n💬 {hitokoto}"
                    
                    # 使用抽象的通知函数发送消息
                    await self.notify_subscribers(final_message)
                else:
                    # 无变化，仅记录日志
                    logger.info(f"🔍 服务器状态无变化: 玩家数 {server_data['online']}/{server_data['max']}")
                    
            except Exception as e:
                logger.error(f"定时监控任务出错: {e}")
                # 出错时等待一下再继续
                await asyncio.sleep(5)



    # 定时任务控制指令
    @filter.command("start_server_monitor")
    async def start_server_monitor_task(self, event: AstrMessageEvent):
        """启动服务器监控任务"""
        if self.task and not self.task.done():
            yield event.plain_result("服务器监控任务已经在运行中")
            return
        
        self.task = asyncio.create_task(self.direct_hello_task())
        logger.info("启动服务器监控任务")
        yield event.plain_result(f"✅ 服务器监控任务已启动，每{self.check_interval}秒检查一次服务器状态")
    
    @filter.command("stop_server_monitor")
    async def stop_server_monitor_task(self, event: AstrMessageEvent):
        """停止服务器监控任务"""
        if self.task and not self.task.done():
            self.task.cancel()
            logger.info("停止服务器监控任务")
            yield event.plain_result("✅ 服务器监控任务已停止")
        else:
            yield event.plain_result("❌ 监控任务未在运行")
    
    @filter.command("查询")
    async def get_server_status(self, event: AstrMessageEvent):
        """立即获取服务器状态"""
        server_info = await self.get_minecraft_server_info()
        
        # 获取一言句子
        hitokoto = await self.get_hitokoto()
        if hitokoto:
            server_info += f"\n\n💬 {hitokoto}"
        
        yield event.plain_result(server_info)
    
    @filter.command("reset_monitor")
    async def reset_monitor(self, event: AstrMessageEvent):
        """重置监控状态缓存"""
        self.last_player_count = None
        self.last_player_list = []
        self.last_status = None
        logger.info("监控状态缓存已重置")
        yield event.plain_result("✅ 监控状态缓存已重置，下次检测将视为首次检测")
    

    
    @filter.command("set_group")
    async def set_target_group(self, event: AstrMessageEvent, group_id: str):
        """设置目标群号"""
        # 验证群号是否为有效数字
        if not group_id.strip().isdigit():
            yield event.plain_result(f"❌ 无效的群号: '{group_id}'。请输入纯数字群号。")
            return
        
        self.target_group = group_id.strip()
        logger.info(f"设置目标群号为: {self.target_group}")
        yield event.plain_result(f"✅ 目标群号已设置为: {self.target_group}")



    async def terminate(self):
        """插件销毁方法"""
        # 停止定时任务
        if self.task and not self.task.done():
            self.task.cancel()
            logger.info("定时发送任务已停止")