import os
import signal
import socket
import sys
import time
import uuid
from functools import partial
from multiprocessing import Process, Queue
from socketserver import BaseRequestHandler, BaseServer
from threading import Lock, Thread
from types import FrameType
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from inference.core import logger
from inference.core.interfaces.camera.video_source import StreamState
from inference.core.interfaces.stream_manager.manager_app.communication import (
    receive_socket_data,
    send_data_trough_socket,
)
from inference.core.interfaces.stream_manager.manager_app.entities import (
    PIPELINE_ID_KEY,
    REPORT_KEY,
    SOURCES_METADATA_KEY,
    STATE_KEY,
    STATUS_KEY,
    TYPE_KEY,
    CommandType,
    ErrorType,
    OperationStatus,
)
from inference.core.interfaces.stream_manager.manager_app.errors import (
    MalformedPayloadError,
)
from inference.core.interfaces.stream_manager.manager_app.inference_pipeline_manager import (
    InferencePipelineManager,
)
from inference.core.interfaces.stream_manager.manager_app.serialisation import (
    describe_error,
    prepare_error_response,
    prepare_response,
)
from inference.core.interfaces.stream_manager.manager_app.tcp_server import (
    RoboflowTCPServer,
)

PROCESSES_TABLE: Dict[str, Tuple[Process, Queue, Queue, Lock]] = {}
HEADER_SIZE = 4
SOCKET_BUFFER_SIZE = 16384
HOST = os.getenv("STREAM_MANAGER_HOST", "127.0.0.1")
PORT = int(os.getenv("STREAM_MANAGER_PORT", "7070"))
SOCKET_TIMEOUT = float(os.getenv("STREAM_MANAGER_SOCKET_TIMEOUT", "5.0"))


class InferencePipelinesManagerHandler(BaseRequestHandler):
    def __init__(
        self,
        request: socket.socket,
        client_address: Any,
        server: BaseServer,
        processes_table: Dict[str, Tuple[Process, Queue, Queue, Lock]],
    ):
        self._processes_table = processes_table  # in this case it's required to set the state of class before superclass init - as it invokes ()
        super().__init__(request, client_address, server)

    def handle(self) -> None:
        pipeline_id: Optional[str] = None
        request_id = str(uuid4())
        try:
            data = receive_socket_data(
                source=self.request,
                header_size=HEADER_SIZE,
                buffer_size=SOCKET_BUFFER_SIZE,
            )
            data[TYPE_KEY] = CommandType(data[TYPE_KEY])
            if data[TYPE_KEY] is CommandType.LIST_PIPELINES:
                return self._list_pipelines(request_id=request_id)
            if data[TYPE_KEY] is CommandType.INIT:
                return self._initialise_pipeline(request_id=request_id, command=data)
            if data[TYPE_KEY] is CommandType.WEBRTC:
                return self._start_webrtc(request_id=request_id, command=data)
            pipeline_id = data[PIPELINE_ID_KEY]
            if data[TYPE_KEY] is CommandType.TERMINATE:
                self._terminate_pipeline(
                    request_id=request_id, pipeline_id=pipeline_id, command=data
                )
            else:
                response = handle_command(
                    processes_table=self._processes_table,
                    request_id=request_id,
                    pipeline_id=pipeline_id,
                    command=data,
                )
                serialised_response = prepare_response(
                    request_id=request_id, response=response, pipeline_id=pipeline_id
                )
                send_data_trough_socket(
                    target=self.request,
                    header_size=HEADER_SIZE,
                    data=serialised_response,
                    request_id=request_id,
                    pipeline_id=pipeline_id,
                )
        except (KeyError, ValueError, MalformedPayloadError) as error:
            logger.error(
                f"Invalid payload in processes manager. error={error} request_id={request_id}..."
            )
            payload = prepare_error_response(
                request_id=request_id,
                error=error,
                error_type=ErrorType.INVALID_PAYLOAD,
                pipeline_id=pipeline_id,
            )
            send_data_trough_socket(
                target=self.request,
                header_size=HEADER_SIZE,
                data=payload,
                request_id=request_id,
                pipeline_id=pipeline_id,
            )
        except Exception as error:
            logger.error(
                f"Internal error in processes manager. error={error} request_id={request_id}..."
            )
            payload = prepare_error_response(
                request_id=request_id,
                error=error,
                error_type=ErrorType.INTERNAL_ERROR,
                pipeline_id=pipeline_id,
            )
            send_data_trough_socket(
                target=self.request,
                header_size=HEADER_SIZE,
                data=payload,
                request_id=request_id,
                pipeline_id=pipeline_id,
            )

    def _list_pipelines(self, request_id: str) -> None:
        serialised_response = prepare_response(
            request_id=request_id,
            response={
                "pipelines": list(self._processes_table.keys()),
                STATUS_KEY: OperationStatus.SUCCESS,
            },
            pipeline_id=None,
        )
        send_data_trough_socket(
            target=self.request,
            header_size=HEADER_SIZE,
            data=serialised_response,
            request_id=request_id,
        )

    def _initialise_pipeline(self, request_id: str, command: dict) -> None:
        pipeline_id = str(uuid4())
        command_queue = Queue()
        responses_queue = Queue()
        inference_pipeline_manager = InferencePipelineManager.init(
            pipeline_id=pipeline_id,
            command_queue=command_queue,
            responses_queue=responses_queue,
        )
        inference_pipeline_manager.start()
        self._processes_table[pipeline_id] = (
            inference_pipeline_manager,
            command_queue,
            responses_queue,
            Lock(),
        )
        command_queue.put((request_id, command))
        response = get_response_ignoring_thrash(
            responses_queue=responses_queue, matching_request_id=request_id
        )
        serialised_response = prepare_response(
            request_id=request_id, response=response, pipeline_id=pipeline_id
        )
        send_data_trough_socket(
            target=self.request,
            header_size=HEADER_SIZE,
            data=serialised_response,
            request_id=request_id,
            pipeline_id=pipeline_id,
        )

    def _start_webrtc(self, request_id: str, command: dict):
        pipeline_id = str(uuid4())
        command_queue = Queue()
        responses_queue = Queue()
        inference_pipeline_manager = InferencePipelineManager.init(
            pipeline_id=pipeline_id,
            command_queue=command_queue,
            responses_queue=responses_queue,
        )
        inference_pipeline_manager.start()
        self._processes_table[pipeline_id] = (
            inference_pipeline_manager,
            command_queue,
            responses_queue,
            Lock(),
        )
        command_queue.put((request_id, command))
        response = get_response_ignoring_thrash(
            responses_queue=responses_queue, matching_request_id=request_id
        )
        serialised_response = prepare_response(
            request_id=request_id, response=response, pipeline_id=pipeline_id
        )
        send_data_trough_socket(
            target=self.request,
            header_size=HEADER_SIZE,
            data=serialised_response,
            request_id=request_id,
            pipeline_id=pipeline_id,
        )

    def _terminate_pipeline(
        self, request_id: str, pipeline_id: str, command: dict
    ) -> None:
        response = handle_command(
            processes_table=self._processes_table,
            request_id=request_id,
            pipeline_id=pipeline_id,
            command=command,
        )
        if response[STATUS_KEY] is OperationStatus.SUCCESS:
            logger.info(
                f"Joining inference pipeline. pipeline_id={pipeline_id} request_id={request_id}"
            )
            join_inference_pipeline(
                processes_table=self._processes_table, pipeline_id=pipeline_id
            )
            logger.info(
                f"Joined inference pipeline. pipeline_id={pipeline_id} request_id={request_id}"
            )
        serialised_response = prepare_response(
            request_id=request_id, response=response, pipeline_id=pipeline_id
        )
        send_data_trough_socket(
            target=self.request,
            header_size=HEADER_SIZE,
            data=serialised_response,
            request_id=request_id,
            pipeline_id=pipeline_id,
        )


def handle_command(
    processes_table: Dict[str, Tuple[Process, Queue, Queue, Lock]],
    request_id: str,
    pipeline_id: str,
    command: dict,
) -> dict:
    if pipeline_id not in processes_table:
        return describe_error(
            exception=None,
            error_type=ErrorType.NOT_FOUND,
            public_error_message=f"Could not find InferencePipeline with id={pipeline_id}.",
        )
    _, command_queue, responses_queue, command_lock = processes_table[pipeline_id]
    with command_lock:
        command_queue.put((request_id, command))
        return get_response_ignoring_thrash(
            responses_queue=responses_queue, matching_request_id=request_id
        )


def get_response_ignoring_thrash(
    responses_queue: Queue, matching_request_id: str
) -> dict:
    while True:
        response = responses_queue.get()
        if response[0] == matching_request_id:
            return response[1]
        logger.warning(
            f"Dropping response for request_id={response[0]} with payload={response[1]}"
        )


def execute_termination(
    signal_number: int,
    frame: FrameType,
    processes_table: Dict[str, Tuple[Process, Queue, Queue, Lock]],
) -> None:
    pipeline_ids = list(processes_table.keys())
    for pipeline_id in pipeline_ids:
        logger.info(f"Terminating pipeline: {pipeline_id}")
        processes_table[pipeline_id][0].terminate()
        logger.info(f"Pipeline: {pipeline_id} terminated.")
        logger.info(f"Joining pipeline: {pipeline_id}")
        processes_table[pipeline_id][0].join()
        logger.info(f"Pipeline: {pipeline_id} joined.")
    logger.info(f"Termination handler completed.")
    sys.exit(0)


def join_inference_pipeline(
    processes_table: Dict[str, Tuple[Process, Queue, Queue, Lock]], pipeline_id: str
) -> None:
    inference_pipeline_manager, *_ = processes_table[pipeline_id]
    inference_pipeline_manager.join()
    del processes_table[pipeline_id]


def check_process_health() -> None:
    while True:
        for pipeline_id, (process, *_) in list(PROCESSES_TABLE.items()):
            if not process.is_alive():
                logger.warning(
                    "Process for pipeline_id=%s is not alive. Terminating...",
                    pipeline_id,
                )
                process.terminate()
                process.join()
                del PROCESSES_TABLE[pipeline_id]
                continue
            command = {
                TYPE_KEY: CommandType.STATUS,
                PIPELINE_ID_KEY: pipeline_id,
            }
            response = handle_command(
                processes_table=PROCESSES_TABLE,
                request_id=uuid.uuid4().hex,
                pipeline_id=pipeline_id,
                command=command,
            )
            if (
                REPORT_KEY not in response
                or SOURCES_METADATA_KEY not in response[REPORT_KEY]
            ):
                continue
            all_sources_statues = set(
                source_metadata[STATE_KEY]
                for source_metadata in response[REPORT_KEY][SOURCES_METADATA_KEY]
                if STATE_KEY in source_metadata
            )
            if not all_sources_statues:
                continue
            if all_sources_statues.issubset({StreamState.ENDED, StreamState.ERROR}):
                logger.info(
                    "All sources depleted in pipeline %s, terminating", pipeline_id
                )
                command = {
                    TYPE_KEY: CommandType.TERMINATE,
                    PIPELINE_ID_KEY: pipeline_id,
                }
                response = handle_command(
                    processes_table=PROCESSES_TABLE,
                    request_id=uuid.uuid4().hex,
                    pipeline_id=pipeline_id,
                    command=command,
                )
                if not response.get(STATUS_KEY) == "success":
                    logger.error(
                        "Malformed response returned by termination command, '%s'",
                        response,
                    )
                    continue
                process.join()
                del PROCESSES_TABLE[pipeline_id]
        time.sleep(1)


def start() -> None:
    signal.signal(
        signal.SIGINT, partial(execute_termination, processes_table=PROCESSES_TABLE)
    )
    signal.signal(
        signal.SIGTERM, partial(execute_termination, processes_table=PROCESSES_TABLE)
    )

    # check process health in daemon thread
    Thread(target=check_process_health, daemon=True).start()

    with RoboflowTCPServer(
        server_address=(HOST, PORT),
        handler_class=partial(
            InferencePipelinesManagerHandler, processes_table=PROCESSES_TABLE
        ),
        socket_operations_timeout=SOCKET_TIMEOUT,
    ) as tcp_server:
        logger.info(
            f"Inference Pipeline Processes Manager is ready to accept connections at {(HOST, PORT)}"
        )
        tcp_server.serve_forever()


if __name__ == "__main__":
    start()