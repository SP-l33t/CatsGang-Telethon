import mimetypes
import uuid

import aiohttp
import aiofiles
import asyncio
import functools
import json
import os
import random
import time
from urllib.parse import unquote
from aiocfscrape import CloudflareScraper
from aiohttp_proxy import ProxyConnector
from better_proxy import Proxy
from datetime import timedelta, datetime, timezone

from telethon import TelegramClient
from telethon.errors import *
from telethon.types import InputUser, InputBotAppShortName, InputPeerUser
from telethon.functions import messages, contacts, channels

from .agents import generate_random_user_agent
from bot.config import settings
from typing import Callable
from bot.utils import logger, proxy_utils, config_utils
from bot.exceptions import InvalidSession
from .headers import headers, get_sec_ch_ua


def error_handler(func: Callable):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            await asyncio.sleep(1)

    return wrapper


class Tapper:
    def __init__(self, tg_client: TelegramClient):
        self.tg_client = tg_client
        self.session_name, _ = os.path.splitext(os.path.basename(tg_client.session.filename))
        self.config = config_utils.get_session_config(self.session_name)
        self.proxy = self.config.get('proxy', None)
        self.tg_web_data = None
        self.tg_client_id = 0
        self.headers = headers
        self.headers['User-Agent'] = self.check_user_agent()
        self.headers.update(**get_sec_ch_ua(self.headers.get('User-Agent', '')))

    def check_user_agent(self):
        user_agent = self.config.get('user_agent')
        if not user_agent:
            user_agent = generate_random_user_agent()
            self.config['user_agent'] = user_agent
            config_utils.update_config_file(self.session_name, self.config)

        return user_agent

    async def get_tg_web_data(self) -> [str | None, str | None]:

        if self.proxy:
            proxy = Proxy.from_str(self.proxy)
            proxy_dict = proxy_utils.to_telethon_proxy(proxy)
        else:
            proxy_dict = None

        self.tg_client.set_proxy(proxy_dict)
        try:
            if not self.tg_client.is_connected():
                try:
                    await self.tg_client.connect()
                except (UnauthorizedError, AuthKeyUnregisteredError):
                    raise InvalidSession(self.session_name)
                except (UserDeactivatedError, UserDeactivatedBanError, PhoneNumberBannedError):
                    raise InvalidSession(f"{self.session_name}: User is banned")

            while True:
                try:
                    resolve_result = await self.tg_client(contacts.ResolveUsernameRequest(username='catsgang_bot'))
                    peer = InputPeerUser(user_id=resolve_result.peer.user_id,
                                         access_hash=resolve_result.users[0].access_hash)
                    break
                except FloodWaitError as fl:
                    fls = fl.seconds

                    logger.warning(f"{self.session_name} | FloodWait {fl}")
                    logger.info(f"{self.session_name} | Sleep {fls}s")
                    await asyncio.sleep(fls + 3)

            ref_id = settings.REF_ID if random.randint(0, 100) <= 85 else "LYfX1AbKvihNGhaOSssv2"

            input_user = InputUser(user_id=resolve_result.peer.user_id, access_hash=resolve_result.users[0].access_hash)
            input_bot_app = InputBotAppShortName(bot_id=input_user, short_name="join")

            web_view = await self.tg_client(messages.RequestAppWebViewRequest(
                peer=peer,
                app=input_bot_app,
                platform='android',
                write_allowed=True,
                start_param=ref_id
            ))

            auth_url = web_view.url
            tg_web_data = unquote(
                string=auth_url.split('tgWebAppData=', maxsplit=1)[1].split('&tgWebAppVersion', maxsplit=1)[0])

            me = await self.tg_client.get_me()
            self.tg_client_id = me.id

            if self.tg_client.is_connected():
                await self.tg_client.disconnect()

            return ref_id, tg_web_data

        except InvalidSession as err:
            logger.error(f"{self.session_name} | Invalid session")
            return None, None

        except Exception as error:
            logger.error(f"{self.session_name} | Unknown error: {error}")
            return None, None

    @error_handler
    async def make_request(self, http_client, method, endpoint=None, url=None, **kwargs):
        full_url = url or f"https://cats-backend-cxblew-prod.up.railway.app{endpoint or ''}"
        response = await http_client.request(method, full_url, **kwargs)
        response.raise_for_status()
        return await response.json()

    @error_handler
    async def login(self, http_client, init_data, ref_id):
        http_client.headers['Authorization'] = "tma " + init_data
        user = await self.make_request(http_client, 'GET', endpoint="/user")
        if not user:
            await self.make_request(http_client, 'POST', endpoint=f"/user/create?referral_code={ref_id}")
            await asyncio.sleep(2)
            return await self.login(http_client, init_data, ref_id)
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
                    logger.error(f"{self.session_name} | Failed to fetch image from cataas.com")
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
                logger.info(
                    f"{self.session_name} | Time until next avatar upload: <y>{hours}</y> hours, <y>{minutes}</y> minutes, and <y>{seconds}</y> seconds")
                return None

    async def join_and_mute_tg_channel(self, link: str):
        path = link.replace('https://t.me/', '')
        if path == 'money':
            return

        async with self.tg_client as client:

            if path.startswith('+'):
                try:
                    invite_hash = path[1:]
                    result = await client(messages.ImportChatInviteRequest(hash=invite_hash))
                    logger.info(f"{self.session_name} | Joined to channel: <y>{result.chats[0].title}</y>")
                    await asyncio.sleep(random.uniform(10, 20))

                except Exception as e:
                    logger.error(f"{self.session_name} | (Task) Error while join tg channel: {e}")
            else:
                try:
                    await client(channels.JoinChannelRequest(channel=f'@{path}'))
                    logger.info(f"{self.session_name} | Joined to channel: <y>{link}</y>")
                except Exception as e:
                    logger.error(f"{self.session_name} | (Task) Error while join tg channel: {e}")

    @error_handler
    async def get_tasks(self, http_client):
        return await self.make_request(http_client, 'GET', endpoint="/tasks/user", data={'group': 'cats'})

    @error_handler
    async def done_tasks(self, http_client, task_id, type_):
        return await self.make_request(http_client, 'POST', endpoint=f"/tasks/{task_id}/{type_}", json={})

    async def check_proxy(self, http_client: aiohttp.ClientSession, proxy: str) -> bool:
        try:
            response = await http_client.get(url='https://httpbin.org/ip', timeout=aiohttp.ClientTimeout(5))
            ip = (await response.json()).get('origin')
            logger.info(f"<light-yellow>{self.session_name}</light-yellow> | Proxy IP: {ip}")
            return True
        except Exception as error:
            logger.error(f"<light-yellow>{self.session_name}</light-yellow> | Proxy: {proxy} | Error: {error}")
            return False

    @error_handler
    async def run(self) -> None:
        if settings.USE_RANDOM_DELAY_IN_RUN:
            random_delay = random.randint(settings.RANDOM_DELAY_IN_RUN[0], settings.RANDOM_DELAY_IN_RUN[1])
            logger.info(f"{self.session_name} | Bot will start in <y>{random_delay}s</y>")
            await asyncio.sleep(random_delay)

        proxy_conn = None
        if self.proxy:
            proxy_conn = ProxyConnector().from_url(self.proxy)
            http_client = CloudflareScraper(headers=self.headers, connector=proxy_conn)
            p_type = proxy_conn._proxy_type
            p_host = proxy_conn._proxy_host
            p_port = proxy_conn._proxy_port
            if not await self.check_proxy(http_client=http_client, proxy=f"{p_type}://{p_host}:{p_port}"):
                return
        else:
            http_client = CloudflareScraper(headers=self.headers)

        ref_id, init_data = await self.get_tg_web_data()

        if not init_data:
            if not http_client.closed:
                await http_client.close()
            if proxy_conn and not proxy_conn.closed:
                proxy_conn.close()
            return

        while True:
            try:
                if http_client.closed:
                    if proxy_conn and not proxy_conn.closed:
                        proxy_conn.close()

                    proxy_conn = ProxyConnector().from_url(self.proxy) if self.proxy else None
                    http_client = aiohttp.ClientSession(headers=self.headers, connector=proxy_conn)

                user_data = await self.login(http_client=http_client, init_data=init_data, ref_id=ref_id)
                if not user_data:
                    logger.error(f"{self.session_name} | Failed to login")
                    await http_client.close()
                    if proxy_conn and not proxy_conn.closed:
                        proxy_conn.close()
                    continue

                logger.info(f"{self.session_name} | <y>Successfully logged in</y>")
                logger.info(
                    f"{self.session_name} | User ID: <y>{user_data.get('id')}</y> | Telegram Age: <y>{user_data.get('telegramAge')}</y> | Points: <y>{user_data.get('totalRewards')}</y>")
                data_task = await self.get_tasks(http_client=http_client)
                if data_task is not None and data_task.get('tasks', {}):
                    for task in data_task.get('tasks'):
                        if task['completed'] is True:
                            continue
                        id = task.get('id')
                        type = task.get('type')
                        title = task.get('title')
                        reward = task.get('rewardPoints')
                        type_ = 'check' if type == 'SUBSCRIBE_TO_CHANNEL' else 'complete'
                        if type_ == 'check':
                            # TODO uncomment if they start checking channel subscription
                            # await self.join_and_mute_tg_channel(link=task.get('params').get('channelUrl'))
                            await asyncio.sleep(2)
                        done_task = await self.done_tasks(http_client=http_client, task_id=id, type_=type_)
                        if done_task and (done_task.get('success', False) or done_task.get('completed', False)):
                            logger.info(f"{self.session_name} | Task <y>{title}</y> done! Reward: {reward}")

                else:
                    logger.error(f"{self.session_name} | No tasks")

                reward = await self.send_cats(http_client=http_client)
                if reward:
                    logger.info(f"{self.session_name} | Reward from Avatar quest: <y>{reward}</y>")

                await http_client.close()
                if proxy_conn and not proxy_conn.closed:
                    proxy_conn.close()

            except InvalidSession as error:
                return

            except Exception as error:
                logger.error(f"{self.session_name} | Unknown error: {error}")
                await asyncio.sleep(delay=3)

            sleep_time = random.randint(settings.SLEEP_TIME[0], settings.SLEEP_TIME[1])
            logger.info(f"{self.session_name} | Sleep <y>{sleep_time}s</y>")
            await asyncio.sleep(delay=sleep_time)


async def run_tapper(tg_client: TelegramClient):
    try:
        await Tapper(tg_client=tg_client).run()
    except InvalidSession:
        session_name, _ = os.path.splitext(os.path.basename(tg_client.session.filename))
        logger.error(f"{session_name} | Invalid Session")
