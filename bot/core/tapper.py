import aiohttp
import asyncio
import fasteners
import functools
import uuid
import os
import random
from urllib.parse import unquote
from aiocfscrape import CloudflareScraper
from aiohttp_proxy import ProxyConnector
from better_proxy import Proxy
from datetime import timedelta, datetime, timezone
from time import time

from telethon import TelegramClient
from telethon.errors import *
from telethon.types import InputUser, InputBotAppShortName, InputPeerUser, InputPeerNotifySettings, InputNotifyPeer
from telethon.functions import messages, contacts, channels, account

from .agents import generate_random_user_agent
from bot.config import settings
from typing import Callable
from bot.utils import logger, log_error, proxy_utils, config_utils, CONFIG_PATH
from bot.exceptions import InvalidSession
from .headers import headers, get_sec_ch_ua


def error_handler(func: Callable):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            # if settings.DEBUG_LOGGING:
            #     log_error(e)
            await asyncio.sleep(1)

    return wrapper


class Tapper:
    def __init__(self, tg_client: TelegramClient):
        self.tg_client = tg_client
        self.session_name, _ = os.path.splitext(os.path.basename(tg_client.session.filename))
        self.config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
        self.proxy = self.config.get('proxy', None)
        self.lock = fasteners.InterProcessLock(os.path.join(os.path.dirname(CONFIG_PATH), 'lock_files',  f"{self.session_name}.lock"))
        self.tg_web_data = None
        self.tg_client_id = 0
        self.headers = headers
        self.headers['User-Agent'] = self.check_user_agent()
        self.headers.update(**get_sec_ch_ua(self.headers.get('User-Agent', '')))

        self._webview_data = None

        if self.proxy:
            proxy = Proxy.from_str(self.proxy)
            proxy_dict = proxy_utils.to_telethon_proxy(proxy)
            self.tg_client.set_proxy(proxy_dict)

    def log_message(self, message) -> str:
        return f"<light-yellow>{self.session_name}</light-yellow> | {message}"

    def check_user_agent(self):
        user_agent = self.config.get('user_agent')
        if not user_agent:
            user_agent = generate_random_user_agent()
            self.config['user_agent'] = user_agent
            config_utils.update_session_config_in_file(self.session_name, self.config, CONFIG_PATH)

        return user_agent

    async def initialize_webview_data(self):
        if not self._webview_data:
            while True:
                try:
                    peer = await self.tg_client.get_input_entity('catsgang_bot')
                    input_bot_app = InputBotAppShortName(bot_id=peer, short_name="join")
                    self._webview_data = {'peer': peer, 'app': input_bot_app}
                    break
                except FloodWaitError as fl:
                    fls = fl.seconds

                    logger.warning(self.log_message(f"FloodWait {fl}. Waiting {fls}s"))
                    await asyncio.sleep(fls + 3)

                except (UnauthorizedError, AuthKeyUnregisteredError):
                    raise InvalidSession(f"{self.session_name}: User is unauthorized")

                except (UserDeactivatedError, UserDeactivatedBanError, PhoneNumberBannedError):
                    raise InvalidSession(f"{self.session_name}: User is banned")

    async def get_tg_web_data(self) -> [str | None, str | None]:
        init_data = None, None
        with self.lock:
            try:
                if not self.tg_client.is_connected():
                    await self.tg_client.connect()
                await self.initialize_webview_data()

                ref_id = settings.REF_ID if random.randint(0, 100) <= 85 else "LYfX1AbKvihNGhaOSssv2"

                web_view = await self.tg_client(messages.RequestAppWebViewRequest(
                    **self._webview_data,
                    platform='android',
                    write_allowed=True,
                    start_param=ref_id
                ))

                auth_url = web_view.url
                tg_web_data = unquote(
                    string=auth_url.split('tgWebAppData=', maxsplit=1)[1].split('&tgWebAppVersion', maxsplit=1)[0])

                me = await self.tg_client.get_me()
                self.tg_client_id = me.id

                init_data = ref_id, tg_web_data

            except InvalidSession:
                raise

            except Exception as error:
                log_error(self.log_message(f"Unknown error during Authorization: {error}"))
                await asyncio.sleep(delay=3)

            finally:
                if self.tg_client.is_connected():
                    await self.tg_client.disconnect()
                    await asyncio.sleep(1)

        return init_data

    @error_handler
    async def make_request(self, http_client, method, endpoint=None, url=None, **kwargs):
        response = await http_client.request(method, url or f"https://api.catshouse.club{endpoint or ''}", **kwargs)
        response.raise_for_status()
        return await response.json()

    @error_handler
    async def login(self, http_client, ref_id):
        user = await self.make_request(http_client, 'GET', endpoint="/user")
        if not user:
            logger.info(self.log_message(f"User not found. Registering..."))
            await self.make_request(http_client, 'POST', endpoint=f"/user/create?referral_code={ref_id}")
            await asyncio.sleep(5)
            user = await self.make_request(http_client, 'GET', endpoint="/user")
        return user

    @error_handler
    async def send_cats(self, http_client):
        avatar_info = await self.make_request(http_client, 'GET', endpoint="/user/avatar")
        if avatar_info:
            attempt_time_str = avatar_info.get('attemptTime', None)
            if not attempt_time_str:
                time_difference = timedelta(hours=25)
            else:
                attempt_time = datetime.fromisoformat(attempt_time_str.replace('Z', '+00:00'))
                current_time = datetime.now(timezone.utc)
                next_day_3am = (attempt_time + timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)

                if current_time >= next_day_3am:
                    time_difference = timedelta(hours=25)
                else:
                    time_difference = next_day_3am - current_time

            if time_difference > timedelta(hours=24):
                response = await http_client.get(f"https://cataas.com/cat?timestamp={int(datetime.now().timestamp() * 1000)}", headers={
                    "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "accept-language": "en-US,en;q=0.9,ru;q=0.8"
                })
                if not response and response.status not in [200, 201]:
                    log_error(self.log_message(f"Failed to fetch image from cataas.com"))
                    return None
                
                image_content = await response.read()

                boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
                form_data = (
                    f'--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="photo"; filename="{uuid.uuid4().hex}.jpg"\r\n'
                    f'Content-Type: image/jpeg\r\n\r\n'
                ).encode('utf-8')
                
                form_data += image_content
                form_data += f'\r\n--{boundary}--\r\n'.encode('utf-8')

                headers = http_client.headers.copy()
                headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
                response = await self.make_request(http_client, 'POST', endpoint="/user/avatar/upgrade", data=form_data, headers=headers)
                if response:
                    return response.get('rewards', 0)
                else:
                    return None
            else:
                hours, remainder = divmod(time_difference.seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                logger.info(self.log_message(
                    f"Time until next avatar upload: <y>{hours}</y> hours, <y>{minutes}</y> minutes, and <y>{seconds}</y> seconds"))
                return None

    async def join_and_mute_tg_channel(self, link: str):
        path = link.replace("https://t.me/", "")
        if path == 'money':
            return

        with self.lock:
            async with self.tg_client as client:
                try:
                    if path.startswith('+'):
                        invite_hash = path[1:]
                        result = await client(messages.ImportChatInviteRequest(hash=invite_hash))
                        channel_title = result.chats[0].title
                        entity = result.chats[0]
                    else:
                        entity = await client.get_entity(f'@{path}')
                        await client(channels.JoinChannelRequest(channel=entity))
                        channel_title = entity.title

                    await asyncio.sleep(1)

                    await client(account.UpdateNotifySettingsRequest(
                        peer=InputNotifyPeer(entity),
                        settings=InputPeerNotifySettings(
                            show_previews=False,
                            silent=True,
                            mute_until=datetime.today() + timedelta(days=365)
                        )
                    ))

                    logger.info(self.log_message(f"Subscribe to channel: <y>{channel_title}</y>"))
                except Exception as e:
                    log_error(self.log_message(f"(Task) Error while subscribing to tg channel: {e}"))

    @error_handler
    async def get_tasks(self, http_client):
        return await self.make_request(http_client, 'GET', endpoint="/tasks/user", data={'group': 'cats'})

    @error_handler
    async def done_tasks(self, http_client, task_id, type_):
        return await self.make_request(http_client, 'POST', endpoint=f"/tasks/{task_id}/{type_}", json={})

    async def check_proxy(self, http_client: aiohttp.ClientSession) -> bool:
        proxy_conn = http_client.connector
        if proxy_conn and not hasattr(proxy_conn, '_proxy_host'):
            logger.info(self.log_message(f"Running Proxy-less"))
            return True
        try:
            response = await http_client.get(url='https://ifconfig.me/ip', timeout=aiohttp.ClientTimeout(15))
            logger.info(self.log_message(f"Proxy IP: {await response.text()}"))
            return True
        except Exception as error:
            proxy_url = f"{proxy_conn._proxy_type}://{proxy_conn._proxy_host}:{proxy_conn._proxy_port}"
            log_error(self.log_message(f"Proxy: {proxy_url} | Error: {type(error).__name__}"))
            return False

    @error_handler
    async def run(self) -> None:
        if settings.USE_RANDOM_DELAY_IN_RUN:
            random_delay = random.randint(settings.RANDOM_DELAY_IN_RUN[0], settings.RANDOM_DELAY_IN_RUN[1])
            logger.info(self.log_message(f"Bot will start in <y>{random_delay}s</y>"))
            await asyncio.sleep(random_delay)

        access_token_created_time = 0
        init_data = None
        token_live_time = random.randint(3500, 3600)

        token_expiration = 0

        proxy_conn = {'connector': ProxyConnector.from_url(self.proxy)} if self.proxy else {}
        async with CloudflareScraper(headers=self.headers, timeout=aiohttp.ClientTimeout(60), **proxy_conn) as http_client:
            while True:
                if not await self.check_proxy(http_client=http_client):
                    logger.warning(self.log_message('Failed to connect to proxy server. Sleep 5 minutes.'))
                    await asyncio.sleep(300)
                    continue

                try:
                    if time() - access_token_created_time >= token_live_time:
                        ref_id, init_data = await self.get_tg_web_data()

                    if not init_data:
                        logger.warning(self.log_message('Failed to get webview URL'))
                        await asyncio.sleep(300)
                        continue

                    access_token_created_time = time()

                    http_client.headers['Authorization'] = f"tma {init_data}"

                    user_data = await self.login(http_client=http_client, ref_id=ref_id)
                    if not user_data:
                        logger.error(f"{self.session_name} | Failed to login. Sleep <y>300s</y>")
                        await asyncio.sleep(delay=300)
                        continue

                    logger.info(self.log_message(f"<y>Successfully logged in</y>"))
                    logger.info(self.log_message(
                        f"User ID: <y>{user_data.get('id')}</y> | Telegram Age: <y>{user_data.get('telegramAge')}</y> |"
                        f" Points: <y>{user_data.get('totalRewards')}</y>"))

                    UserHasOgPass = user_data.get('hasOgPass', False)
                    logger.info(f"{self.session_name} | User has OG Pass: <y>{UserHasOgPass}</y>")

                    data_task = await self.get_tasks(http_client=http_client)
                    if data_task is not None and data_task.get('tasks', {}):
                        for task in data_task.get('tasks'):
                            if task['completed'] is True:
                                continue
                            task_id = task.get('id')
                            task_type = task.get('type')
                            title = task.get('title')
                            reward = task.get('rewardPoints')
                            type_ = 'check' if task_type == 'SUBSCRIBE_TO_CHANNEL' else 'complete'
                            if type_ == 'check':
                                # TODO uncomment if they start checking channel subscription
                                # await self.join_and_mute_tg_channel(link=task.get('params').get('channelUrl'))
                                await asyncio.sleep(2)
                            done_task = await self.done_tasks(http_client=http_client, task_id=task_id, type_=type_)
                            if done_task and (done_task.get('success', False) or done_task.get('completed', False)):
                                logger.info(self.log_message(f"Task <y>{title}</y> done! Reward: {reward}"))

                    else:
                        logger.warning(self.log_message(f" No tasks"))

                    for _ in range(3 if UserHasOgPass else 1):
                        reward = await self.send_cats(http_client=http_client)
                        if reward:
                            logger.info(self.log_message(f"Reward from Avatar quest: <y>{reward}</y>"))
                        await asyncio.sleep(random.randint(5, 7))

                except InvalidSession as error:
                    return

                except Exception as error:
                    log_error(self.log_message(f"Unknown error: {error}"))
                    await asyncio.sleep(delay=3)

                sleep_time = random.randint(settings.SLEEP_TIME[0], settings.SLEEP_TIME[1])
                logger.info(f"{self.session_name} | Sleep <y>{sleep_time}s</y>")
                await asyncio.sleep(delay=sleep_time)


async def run_tapper(tg_client: TelegramClient):
    runner = Tapper(tg_client=tg_client)
    try:
        await runner.run()
    except InvalidSession as e:
        logger.error(runner.log_message(f"Invalid Session: {e}"))
