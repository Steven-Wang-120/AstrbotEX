"""Actual A.E.B. plugin/Host tools with a minimal registration-only test context.

No mock ROUTER or mock buffers. No paid LLM call. HTTP is localhost-only test IO.
"""
import asyncio
import json
from pathlib import Path
import sys
import os
from aiohttp import web

sys.path.insert(0,os.environ['AEB_CHECKOUT'])
from astrbot_plugin_astrbotex_interaction.main import AstrBotEXInteractionPlugin


class HarnessContext:
    def __init__(self):
        self.platform_manager=type('Platforms',(),{'platform_insts':[]})()
        self.tools=[]
    def add_llm_tools(self,*tools):self.tools.extend(tools)
    def get_using_stt_provider(self):return None
    def get_config(self):return {'provider_tts_settings':{'provider_id':''}}
    def get_provider_by_id(self,_):return None
    def get_all_tts_providers(self):return []


async def main():
    ctx=HarnessContext()
    plugin=AstrBotEXInteractionPlugin(ctx)
    await plugin.initialize()
    tools={t.name:t for t in ctx.tools}
    async def snapshot(request):
        stream=request.query.get('stream')
        j=await tools['get_astrbotex_vision_json_buffer'].call(None,stream_id=stream,limit=8)
        image=await tools['get_astrbotex_vision_jpeg_buffer'].call(None,stream_id=stream)
        return web.json_response({'json':j.model_dump(mode='json'),'jpeg':image.model_dump(mode='json'),
                                  'tools':list(tools),'receiver':'real A.E.B. and real AstrBot FunctionTool APIs; harness context'})
    async def mark(request):
        result=await tools['set_astrbotex_vision_marks'].call(None,**await request.json())
        return web.Response(text=result,content_type='application/json')
    app=web.Application()
    app.router.add_get('/buffers',snapshot)
    app.router.add_post('/mark',mark)
    runner=web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner,'127.0.0.1',18866).start()
    print('AEB_HOST_READY',flush=True)
    try:await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await plugin.terminate()


if __name__=='__main__':asyncio.run(main())
