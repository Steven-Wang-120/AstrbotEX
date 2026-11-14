"""Synthetic media echo plugin, installed only in the disposable AstrBot lab."""
import base64
import hashlib
from pathlib import Path
import aiohttp

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp


@register('astrc_media_probe', 'astrC tests', 'Disposable media acceptance probe', '1.0.0')
class Main(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command('media_probe')
    async def media_probe(self, event: AstrMessageEvent):
        for part in event.get_messages():
            if isinstance(part, Comp.Image):
                # Read the original signed download inside the real Agent. Its
                # preprocessing can replace the component URL with a local JPEG.
                original = next(segment['data']['url'] for segment in event.message_obj.raw_message['message'] if segment['type'] == 'image')
                async with aiohttp.ClientSession() as session:
                    async with session.get(original) as response:
                        response.raise_for_status()
                        raw = await response.read()
                marker = 'media-probe:' + hashlib.sha256(raw).hexdigest()
                yield event.chain_result([Comp.Plain(marker), Comp.Image.fromBytes(raw)])
            elif isinstance(part, Comp.File):
                path = Path(await part.get_file())
                raw = path.read_bytes()
                marker = 'media-probe:' + hashlib.sha256(raw).hexdigest()
                # EX intentionally accepts base64 only; ordinary file:// plugins
                # remain outside the supported outbound subset.
                await event.bot.call_action('send_private_msg', user_id=int(event.get_sender_id()), message=[
                    {'type': 'text', 'data': {'text': marker}},
                    {'type': 'file', 'data': {'name': part.name, 'file': 'base64://' + base64.b64encode(raw).decode()}},
                ])
