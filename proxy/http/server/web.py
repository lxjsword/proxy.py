# -*- coding: utf-8 -*-
"""
    proxy.py
    ~~~~~~~~
    ⚡⚡⚡ Fast, Lightweight, Pluggable, TLS interception capable proxy server focused on
    Network monitoring, controls & Application development, testing, debugging.

    :copyright: (c) 2013-present by Abhinav Singh and contributors.
    :license: BSD, see LICENSE for more details.
"""
import re
import time
import socket
import logging
from typing import Any, Dict, List, Tuple, Union, Pattern, Optional

from .plugin import HttpWebServerBasePlugin
from ..parser import HttpParser, httpParserTypes
from ..plugin import HttpProtocolHandlerPlugin
from ..methods import httpMethods
from .protocols import httpProtocolTypes
from ..exception import HttpProtocolException
from ..protocols import httpProtocols
from ..responses import NOT_FOUND_RESPONSE_PKT
from ..websocket import WebsocketFrame, websocketOpcodes
from ...core.event import eventNames
from ...common.flag import flags
from ...common.types import Readables, Writables, Descriptors
from ...common.utils import text_, build_websocket_handshake_response
from ...common.constants import (
    DEFAULT_ENABLE_WEB_SERVER, DEFAULT_STATIC_SERVER_DIR,
    DEFAULT_ENABLE_REVERSE_PROXY, DEFAULT_ENABLE_STATIC_SERVER,
    DEFAULT_WEB_ACCESS_LOG_FORMAT, DEFAULT_MIN_COMPRESSION_LENGTH,
)


logger = logging.getLogger(__name__)


flags.add_argument(
    '--enable-web-server',
    action='store_true',
    default=DEFAULT_ENABLE_WEB_SERVER,
    help='Default: False.  Whether to enable proxy.HttpWebServerPlugin.',
)

flags.add_argument(
    '--enable-static-server',
    action='store_true',
    default=DEFAULT_ENABLE_STATIC_SERVER,
    help='Default: False.  Enable inbuilt static file server. '
    'Optionally, also use --static-server-dir to serve static content '
    'from custom directory.  By default, static file server serves '
    'out of installed proxy.py python module folder.',
)

flags.add_argument(
    '--static-server-dir',
    type=str,
    default=DEFAULT_STATIC_SERVER_DIR,
    help='Default: "public" folder in directory where proxy.py is placed. '
    'This option is only applicable when static server is also enabled. '
    'See --enable-static-server.',
)

flags.add_argument(
    '--min-compression-length',
    type=int,
    default=DEFAULT_MIN_COMPRESSION_LENGTH,
    help='Default: ' + str(DEFAULT_MIN_COMPRESSION_LENGTH) + ' bytes.  ' +
    'Sets the minimum length of a response that will be compressed (gzipped).',
)

flags.add_argument(
    '--enable-reverse-proxy',
    action='store_true',
    default=DEFAULT_ENABLE_REVERSE_PROXY,
    help='Default: False.  Whether to enable reverse proxy core.',
)


class HttpWebServerPlugin(HttpProtocolHandlerPlugin):
    """HttpProtocolHandler plugin which handles incoming requests to local web server."""

    def __init__(
            self,
            *args: Any, **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.start_time: float = time.time()
        self.pipeline_request: Optional[HttpParser] = None
        self.switched_protocol: Optional[int] = None
        self.route: Optional[HttpWebServerBasePlugin] = None

        self.plugins: Dict[str, HttpWebServerBasePlugin] = {}
        self.routes: Dict[
            int, Dict[Pattern[str], HttpWebServerBasePlugin],
        ] = {
            httpProtocolTypes.HTTP: {},
            httpProtocolTypes.HTTPS: {},
            httpProtocolTypes.WEBSOCKET: {},
        }
        if b'HttpWebServerBasePlugin' in self.flags.plugins:
            self._initialize_web_plugins()

        self._response_size = 0
        self._post_request_data_size = 0

    @staticmethod
    def protocols() -> List[int]:
        return [httpProtocols.WEB_SERVER]

    def _initialize_web_plugins(self) -> None:
        for klass in self.flags.plugins[b'HttpWebServerBasePlugin']:
            instance: HttpWebServerBasePlugin = klass(
                self.uid,
                self.flags,
                self.client,
                self.event_queue,
                self.upstream_conn_pool,
            )
            self.plugins[instance.name()] = instance
            for (protocol, route) in instance.routes():
                pattern = re.compile(route)
                self.routes[protocol][pattern] = self.plugins[instance.name()]

    def encryption_enabled(self) -> bool:
        return self.flags.keyfile is not None and \
            self.flags.certfile is not None

    def switch_to_websocket(self) -> None:
        self.client.queue(
            memoryview(
                build_websocket_handshake_response(
                    WebsocketFrame.key_to_accept(
                        self.request.header(b'Sec-WebSocket-Key'),
                    ),
                ),
            ),
        )
        self.switched_protocol = httpProtocolTypes.WEBSOCKET

    def on_request_complete(self) -> Union[socket.socket, bool]:
        self.emit_request_complete()
        path = self.request.path or b'/'
        teardown = self._try_route(path)
        if teardown:
            return teardown
        # No-route found, try static serving if enabled
        if self.route is None:
            if self.flags.enable_static_server:
                self._try_static_or_404(path)
                return True
            # Catch all unhandled web server requests, return 404
            self.client.queue(NOT_FOUND_RESPONSE_PKT)
            return True
        return False

    async def get_descriptors(self) -> Descriptors:
        r, w = [], []
        for plugin in self.plugins.values():
            r1, w1 = await plugin.get_descriptors()
            r.extend(r1)
            w.extend(w1)
        return r, w

    async def write_to_descriptors(self, w: Writables) -> bool:
        for plugin in self.plugins.values():
            teardown = await plugin.write_to_descriptors(w)
            if teardown:
                return True
        return False

    async def read_from_descriptors(self, r: Readables) -> bool:
        for plugin in self.plugins.values():
            teardown = await plugin.read_from_descriptors(r)
            if teardown:
                return True
        return False

    def on_client_data(self, raw: memoryview) -> None:
        self._post_request_data_size += len(raw)
        if self.route and self.route.on_client_data(self.request, raw) is None:
            return
        if self.switched_protocol == httpProtocolTypes.WEBSOCKET:
            # TODO(abhinavsingh): Do we really tobytes() here?
            # Websocket parser currently doesn't depend on internal
            # buffers, due to which it can directly parse out of
            # memory views.  But how about large payloads scenarios?
            remaining = raw.tobytes()
            frame = WebsocketFrame()
            while remaining != b'':
                # TODO: Tear down if invalid protocol exception
                remaining = frame.parse(remaining)
                if frame.opcode == websocketOpcodes.CONNECTION_CLOSE:
                    raise HttpProtocolException(
                        'Client sent connection close packet',
                    )
                else:
                    assert self.route
                    self.route.on_websocket_message(frame)
                frame.reset()
            return
        # If 1st valid request was completed and it's a HTTP/1.1 keep-alive
        # And only if we have a route, parse pipeline requests
        if self.request.is_complete and \
                self.request.is_http_1_1_keep_alive and \
                self.route is not None:
            if self.pipeline_request is None:
                self.pipeline_request = HttpParser(
                    httpParserTypes.REQUEST_PARSER,
                )
            self.pipeline_request.parse(raw)
            if self.pipeline_request.is_complete:
                self.route.handle_request(self.pipeline_request)
                if not self.pipeline_request.is_http_1_1_keep_alive:
                    raise HttpProtocolException(
                        'Pipelined request is not keep-alive, will tear down request...',
                    )
                self.pipeline_request = None

    def on_response_chunk(self, chunk: List[memoryview]) -> List[memoryview]:
        self._response_size += sum(len(c) for c in chunk)
        return chunk

    def _context(self) -> Dict[str, Any]:
        return {
            'client_ip': None if not self.client.addr else self.client.addr[0],
            'client_port': None if not self.client.addr else self.client.addr[1],
            'connection_time_ms': '%.2f' % ((time.time() - self.start_time) * 1000),
            # Request
            'request_method': text_(self.request.method),
            'request_path': text_(self.request.path),
            'request_bytes': self.request.total_size + self._post_request_data_size,
            'request_ua': (
                text_(self.request.header(b'user-agent'))
                if self.request.has_header(b'user-agent')
                else None
            ),
            'request_version': (
                None if not self.request.version else text_(self.request.version)
            ),
            # Response
            #
            # TODO: Track and inject web server specific response attributes
            # Currently, plugins are allowed to queue raw bytes, because of
            # which we'll have to reparse the queued packets to deduce
            # several attributes required below.  At least for code and
            # reason attributes.
            #
            'response_bytes': self._response_size,
            # 'response_code': text_(self.response.code),
            # 'response_reason': text_(self.response.reason),
        }

    def on_client_connection_close(self) -> None:
        context = self._context()
        log_handled = False
        if self.route:
            # May be merge on_client_connection_close and on_access_log???
            # probably by simply deprecating on_client_connection_close in future.
            self.route.on_client_connection_close()
            ctx = self.route.on_access_log(context)
            if ctx is None:
                log_handled = True
            else:
                context = ctx
        if not log_handled:
            self.access_log(context)

    def access_log(self, context: Dict[str, Any]) -> None:
        logger.info(DEFAULT_WEB_ACCESS_LOG_FORMAT.format_map(context))

    @property
    def _protocol(self) -> Tuple[bool, int]:
        do_ws_upgrade = self.request.is_websocket_upgrade
        return do_ws_upgrade, httpProtocolTypes.WEBSOCKET \
            if do_ws_upgrade \
            else httpProtocolTypes.HTTPS \
            if self.encryption_enabled() \
            else httpProtocolTypes.HTTP

    def _try_route(self, path: bytes) -> bool:
        do_ws_upgrade, protocol = self._protocol
        for route in self.routes[protocol]:
            if route.match(text_(path)):
                self.route = self.routes[protocol][route]
                assert self.route
                # Optionally, upgrade protocol
                if do_ws_upgrade and self.route.do_upgrade(self.request):
                    self.switch_to_websocket()
                    assert self.route
                    # Invoke plugin.on_websocket_open
                    self.route.on_websocket_open()
                else:
                    # Invoke plugin.handle_request
                    self.route.handle_request(self.request)
                    # if self.request.has_header(b'connection') and \
                    #         self.request.header(b'connection').lower() == b'close':
                    #     return True
                # Bailout on first match
                break
        return False

    def _try_static_or_404(self, path: bytes) -> None:
        path = text_(path).split('?', 1)[0]
        self.client.queue(
            HttpWebServerBasePlugin.serve_static_file(
                self.flags.static_server_dir + path,
                self.flags.min_compression_length,
            ),
        )

    def emit_request_complete(self) -> None:
        if not self.flags.enable_events:
            return
        assert self.request.port and self.event_queue
        self.event_queue.publish(
            request_id=self.uid,
            event_name=eventNames.REQUEST_COMPLETE,
            event_payload={
                'url': 'http://%s%s'
                % (
                    text_(self.request.header(b'host')),
                    text_(self.request.path),
                ),
                'method': text_(self.request.method),
                'headers': (
                    {}
                    if not self.request.headers
                    else {
                        text_(k): text_(v[1]) for k, v in self.request.headers.items()
                    }
                ),
                'body': (
                    text_(self.request.body, errors='ignore')
                    if self.request.method == httpMethods.POST
                    else None
                ),
            },
            publisher_id=self.__class__.__qualname__,
        )
