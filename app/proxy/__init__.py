from app.proxy.mock import PityMock
from app.proxy.record import PityRecorder
from config import Config


async def start_proxy(log):
    """
    start mitmproxy server
    :return:
    """
    try:
        from mitmproxy import options
        from mitmproxy.tools.dump import DumpMaster
    except ImportError:
        log.bind(name=None).warning(
            "mitmproxy not installed, Please see: https://docs.mitmproxy.org/stable/overview-installation/")
        return

    addons = [
        PityRecorder()
    ]
    if Config.MOCK_ON:
        addons.append(PityMock())
    opts = options.Options(listen_host='0.0.0.0', listen_port=Config.PROXY_PORT)
    m = DumpMaster(opts, False, False)
    m.addons.add(*addons)
    log.bind(name=None).info(f"mock server is running at http://0.0.0.0:{Config.PROXY_PORT}")
    await m.run()
