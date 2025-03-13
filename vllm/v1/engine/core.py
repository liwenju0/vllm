# SPDX-License-Identifier: Apache-2.0

import queue
import signal
import threading
import time
from concurrent.futures import Future
from inspect import isclass, signature
from multiprocessing.connection import Connection
from typing import Any, Optional, Callable, TypeVar, cast

import msgspec
import psutil
import zmq
import zmq.asyncio

from vllm.config import VllmConfig
from vllm.logger import init_logger
from vllm.lora.request import LoRARequest
from vllm.transformers_utils.config import (
    maybe_register_config_serialize_by_value)
from vllm.utils import (get_exception_traceback, resolve_obj_by_qualname,
                        zmq_socket_ctx)
from vllm.v1.core.kv_cache_utils import get_kv_cache_configs
from vllm.v1.core.scheduler import Scheduler as V1Scheduler
from vllm.v1.core.scheduler import SchedulerOutput
from vllm.v1.engine import (EngineCoreOutputs, EngineCoreRequest,
                            EngineCoreRequestType, UtilityOutput)
from vllm.v1.engine.mm_input_cache import MMInputCacheServer
from vllm.v1.executor.abstract import Executor
from vllm.v1.outputs import ModelRunnerOutput
from vllm.v1.request import Request, RequestStatus
from vllm.v1.serial_utils import MsgpackDecoder, MsgpackEncoder
from vllm.v1.structured_output import StructuredOutputManager
from vllm.version import __version__ as VLLM_VERSION

logger = init_logger(__name__)

POLLING_TIMEOUT_S = 2.5

F = TypeVar('F', bound=Callable[..., Any])

def handle_engine_errors(func: F) -> F:
    """
    Decorator to catch exceptions 
    during engine initialization or operations.
    
    This decorator catches exceptions, 
    formats them using the _format_error method,
    and sends the error information 
    through the ready_pipe if available.
    """
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            error_info = self._format_error(e)
            # Send structured error information through ready_pipe if available
            if hasattr(self, 'ready_pipe') and self.ready_pipe:
                self.ready_pipe.send({
                    "status": "error",
                    "error": error_info
                })
            logger.error(f"Engine error: {error_info['type']}: {error_info['message']}")
            raise  # Re-raise the original exception to preserve the stack trace
    return cast(F, wrapper)

class EngineCore:
    """Inner loop of vLLM's Engine."""

    def _format_error(self, exc: Exception) -> dict[str, Any]:
        """Format an exception into a structured error message.
        
        Args:
            exc: The exception to format.
            
        Returns:
            A dictionary containing structured error information.
        """
        error_info = {
            "type": exc.__class__.__name__,
            "message": str(exc),
            "traceback": get_exception_traceback(exc),
            "details": getattr(exc, "details", None)
        }
        
        # Enhance error reporting for specific error types
        self._enhance_error_info(error_info, exc)
        
        return error_info
        
    def _enhance_error_info(self, error_info: dict[str, Any], exc: Exception) -> None:
        """Enhance error information for specific types of errors.
        
        This method adds additional context and recommendations for known error types.
        It's designed to be extensible for future error types.
        
        Args:
            error_info: The error information dictionary to enhance.
            exc: The original exception.
        """
        # Handle CUDA out of memory errors
        if self._is_cuda_out_of_memory_error(exc):
            import torch
            
            # Get basic CUDA memory information
            if torch.cuda.is_available():
                device = torch.cuda.current_device()
                total_memory = torch.cuda.get_device_properties(device).total_memory / (1024**3)
                allocated_memory = torch.cuda.memory_allocated(device) / (1024**3)
                
                # Add detailed memory information
                error_info["details"] = {
                    "cuda_info": {
                        "total_gpu_memory_gb": round(total_memory, 2),
                        "allocated_memory_gb": round(allocated_memory, 2),
                        "available_memory_gb": round(total_memory - allocated_memory, 2),
                        "device_name": torch.cuda.get_device_name(device)
                    },
                    "recommendations": [
                        "Increase 'gpu_memory_utilization' parameter",
                        "Decrease 'max_model_len' parameter",
                        "Try using a smaller model",
                        "Use a GPU with more memory",
                        "Enable tensor parallelism across multiple GPUs"
                    ]
                }
                
                # Update error message to include memory information
                error_info["message"] = (
                    f"CUDA out of memory during initialization. "
                    f"Available GPU memory: {round(total_memory - allocated_memory, 2)}GB "
                    f" / {round(total_memory, 2)}GB "
                    f"on {torch.cuda.get_device_name(device)}. {str(exc)}")
    
    def _is_cuda_out_of_memory_error(self, exc: Exception) -> bool:
        """Determine if an exception is related to CUDA out of memory errors.
        
        Args:
            exc: The exception to check.
            
        Returns:
            True if the exception is a CUDA out of memory error, False otherwise.
        """
        # Check for different types of CUDA OOM errors
        error_msg = str(exc).lower()
        
        # Check ValueErrors from check_enough_kv_cache_memory
        if isinstance(exc, ValueError) and any(x in error_msg for x in [
            "no available memory for the cache blocks",
            "larger than the available kv cache memory"
        ]):
            return True
            
        # Check PyTorch CUDA OOM errors
        if ("cuda out of memory" in error_msg or  
            "cudaerror" in error_msg 
            or "out of memory" in error_msg
        ):
            return True
            
        # Check other memory-related errors
        if isinstance(exc, RuntimeError) and "out of memory" in error_msg:
            return True
            
        return False

    @handle_engine_errors
    def __init__(
        self,
        vllm_config: VllmConfig,
        executor_class: type[Executor],
        log_stats: bool,
    ):
        assert vllm_config.model_config.runner_type != "pooling"

        logger.info("Initializing a V1 LLM engine (v%s) with config: %s",
                    VLLM_VERSION, vllm_config)

        self.log_stats = log_stats

        # Setup Model.
        self.model_executor = executor_class(vllm_config)

        # Setup KV Caches and update CacheConfig after profiling.
        num_gpu_blocks, num_cpu_blocks = self._initialize_kv_caches(
            vllm_config)
        vllm_config.cache_config.num_gpu_blocks = num_gpu_blocks
        vllm_config.cache_config.num_cpu_blocks = num_cpu_blocks

        self.structured_output_manager = StructuredOutputManager(vllm_config)

        # Setup scheduler.
        if isinstance(vllm_config.scheduler_config.scheduler_cls, str):
            Scheduler = resolve_obj_by_qualname(
                vllm_config.scheduler_config.scheduler_cls)
        else:
            Scheduler = vllm_config.scheduler_config.scheduler_cls

        # This warning can be removed once the V1 Scheduler interface is
        # finalized and we can maintain support for scheduler classes that
        # implement it
        if Scheduler is not V1Scheduler:
            logger.warning(
                "Using configured V1 scheduler class %s. "
                "This scheduler interface is not public and "
                "compatibility may not be maintained.",
                vllm_config.scheduler_config.scheduler_cls)

        self.scheduler = Scheduler(
            scheduler_config=vllm_config.scheduler_config,
            model_config=vllm_config.model_config,
            cache_config=vllm_config.cache_config,
            lora_config=vllm_config.lora_config,
            speculative_config=vllm_config.speculative_config,
            log_stats=self.log_stats,
            structured_output_manager=self.structured_output_manager,
        )

        # Setup MM Input Mapper.
        self.mm_input_cache_server = MMInputCacheServer(
            vllm_config.model_config)

        # Setup batch queue for pipeline parallelism.
        # Batch queue for scheduled batches. This enables us to asynchronously
        # schedule and execute batches, and is required by pipeline parallelism
        # to eliminate pipeline bubbles.
        self.batch_queue_size = self.model_executor.max_concurrent_batches
        self.batch_queue: Optional[queue.Queue[tuple[Future[ModelRunnerOutput],
                                                     SchedulerOutput]]] = None
        if self.batch_queue_size > 1:
            logger.info("Batch queue is enabled with size %d",
                        self.batch_queue_size)
            self.batch_queue = queue.Queue(self.batch_queue_size)

    @handle_engine_errors
    def _initialize_kv_caches(self,
                              vllm_config: VllmConfig) -> tuple[int, int]:
        """Initialize Key-Value caches for the engine.
        
        This method is wrapped with error handling 
        to catch and report initialization issues.
        
        Args:
            vllm_config: The VLLM configuration object.
            
        Returns:
            A tuple containing the number of GPU and CPU blocks.
        """
        start = time.time()

        # Get all kv cache needed by the model
        kv_cache_specs = self.model_executor.get_kv_cache_specs()

        # Profiles the peak memory usage of the model to determine how much
        # memory can be allocated for kv cache.
        available_gpu_memory = self.model_executor.determine_available_memory()

        # Get the kv cache tensor size
        kv_cache_configs = get_kv_cache_configs(vllm_config, kv_cache_specs,
                                               available_gpu_memory)
        num_gpu_blocks_set = set(config.num_blocks
                                for config in kv_cache_configs)
        assert len(num_gpu_blocks_set) == 1, (
            f"num_gpu_blocks need to be the same across workers, "
            f"but they are different: {num_gpu_blocks_set}")
        num_gpu_blocks = num_gpu_blocks_set.pop()
        num_cpu_blocks = 0

        # Initialize kv cache and warmup the execution
        self.model_executor.initialize_from_config(kv_cache_configs)

        elapsed = time.time() - start
        logger.info(("init engine (profile, create kv cache, "
                     "warmup model) took %.2f seconds"), elapsed)
        return num_gpu_blocks, num_cpu_blocks

    def add_request(self, request: EngineCoreRequest):
        """Add request to the scheduler."""

        if request.mm_hashes is not None:
            # Here, if hash exists for a multimodal input, then it will be
            # fetched from the cache, else it will be added to the cache.
            # Note that the cache here is mirrored with the client cache, so
            # anything that has a hash must have a HIT cache entry here
            # as well.
            assert request.mm_inputs is not None
            request.mm_inputs = self.mm_input_cache_server.get_and_update(
                request.mm_inputs, request.mm_hashes)

        req = Request.from_engine_core_request(request)
        if req.use_structured_output:
            # Start grammar compilation asynchronously
            self.structured_output_manager.grammar_init(req)

        self.scheduler.add_request(req)

    def abort_requests(self, request_ids: list[str]):
        """Abort requests from the scheduler."""

        # TODO: The scheduler doesn't really need to know the
        # specific finish reason, TBD whether we propagate that
        # (i.e. client-aborted vs stop criteria met).
        self.scheduler.finish_requests(request_ids,
                                       RequestStatus.FINISHED_ABORTED)

    def step(self) -> EngineCoreOutputs:
        """Schedule, execute, and make output."""

        # Check for any requests remaining in the scheduler - unfinished,
        # or finished and not yet removed from the batch.
        if not self.scheduler.has_requests():
            return EngineCoreOutputs(
                outputs=[],
                scheduler_stats=self.scheduler.make_stats(),
            )
        scheduler_output = self.scheduler.schedule()

        # This case may occur when the only unfinished requests are
        # structured output requests where the grammar has not finished
        # compiling yet, so there's nothing to run.
        if scheduler_output.total_num_scheduled_tokens == 0:
            return EngineCoreOutputs(
                outputs=[],
                scheduler_stats=self.scheduler.make_stats(),
            )

        output = self.model_executor.execute_model(scheduler_output)
        engine_core_outputs = self.scheduler.update_from_output(
            scheduler_output, output)  # type: ignore

        return engine_core_outputs

    def step_with_batch_queue(self) -> Optional[EngineCoreOutputs]:
        """Schedule and execute batches with the batch queue.
        Note that if nothing to output in this step, None is returned.

        The execution flow is as follows:
        1. Try to schedule a new batch if there are unscheduled requests
        and the job queue is not full. If a new batch is scheduled, directly
        return an empty engine core output. In other words, we won't check
        and return model outputs before the batch queue is full.
        2. If there is no new scheduled batch, meaning that the batch queue
        is full or no other requests can be scheduled, we block until the first
        batch in the job queue is finished.
        3. Update the scheduler from the output.
        """
        assert self.batch_queue is not None

        engine_core_outputs = None
        scheduler_output = None
        # If there are unscheduled requests and the job queue
        # is not full, schedule a new batch. Note that this is not blocking.
        if (self.scheduler.get_num_unscheduled_requests() > 0
                and not self.batch_queue.full()):
            scheduler_output = self.scheduler.schedule()
            if scheduler_output.total_num_scheduled_tokens > 0:
                future = self.model_executor.execute_model(scheduler_output)
                self.batch_queue.put_nowait(
                    (future, scheduler_output))  # type: ignore

        scheduled_batch = (scheduler_output is not None
                           and scheduler_output.total_num_scheduled_tokens > 0)

        # If no more requests can be scheduled and the job queue is not empty,
        # block until the first batch in the job queue is finished.
        if not scheduled_batch and not self.batch_queue.empty():
            future, scheduler_output = self.batch_queue.get_nowait()
            # Blocking until the first result is available.
            model_output = future.result()
            self.batch_queue.task_done()
            engine_core_outputs = self.scheduler.update_from_output(
                scheduler_output, model_output)

        return engine_core_outputs

    def shutdown(self):
        self.model_executor.shutdown()

    def profile(self, is_start: bool = True):
        self.model_executor.profile(is_start)

    def reset_prefix_cache(self):
        self.scheduler.reset_prefix_cache()

    def sleep(self, level: int = 1):
        self.model_executor.sleep(level)

    def wake_up(self):
        self.model_executor.wake_up()

    def execute_dummy_batch(self):
        self.model_executor.collective_rpc("execute_dummy_batch")

    def add_lora(self, lora_request: LoRARequest) -> bool:
        return self.model_executor.add_lora(lora_request)

    def remove_lora(self, lora_id: int) -> bool:
        return self.model_executor.remove_lora(lora_id)

    def list_loras(self) -> set[int]:
        return self.model_executor.list_loras()

    def pin_lora(self, lora_id: int) -> bool:
        return self.model_executor.pin_lora(lora_id)


class EngineCoreProc(EngineCore):
    """ZMQ-wrapper for running EngineCore in background process."""

    @handle_engine_errors
    def __init__(
        self,
        input_path: str,
        output_path: str,
        ready_pipe: Connection,
        vllm_config: VllmConfig,
        executor_class: type[Executor],
        log_stats: bool,
    ):
        # Store ready_pipe before super().__init__ 
        # so that error handler can access it
        self.ready_pipe = ready_pipe
        
        # Initialize the core engine - errors here 
        # will be caught by the decorator
        super().__init__(vllm_config, executor_class, log_stats)

        # Background Threads and Queues for IO. These enable us to
        # overlap ZMQ socket IO with GPU since they release the GIL,
        # and to overlap some serialization/deserialization with the
        # model forward pass.
        # Threads handle Socket <-> Queues and core_busy_loop uses Queue.
        self.input_queue: queue.Queue[tuple[EngineCoreRequestType,
                                            Any]] = queue.Queue()
        self.output_queue: queue.Queue[EngineCoreOutputs] = queue.Queue()
        threading.Thread(target=self.process_input_socket,
                         args=(input_path, ),
                         daemon=True).start()
        threading.Thread(target=self.process_output_socket,
                         args=(output_path, ),
                         daemon=True).start()

        # Send Readiness signal to EngineClient.
        ready_pipe.send({"status": "READY"})

    @staticmethod
    def run_engine_core(*args, **kwargs):
        """Launch EngineCore busy loop in background process."""

        # Signal handler used for graceful termination.
        # SystemExit exception is only raised once to allow this and worker
        # processes to terminate without error
        shutdown_requested = False

        # Ensure we can serialize transformer config after spawning
        maybe_register_config_serialize_by_value()

        def signal_handler(signum, frame):
            nonlocal shutdown_requested
            if not shutdown_requested:
                shutdown_requested = True
                raise SystemExit()

        # Either SIGTERM or SIGINT will terminate the engine_core
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        parent_process = psutil.Process().parent()
        engine_core = None
        try:
            engine_core = EngineCoreProc(*args, **kwargs)
            engine_core.run_busy_loop()

        except SystemExit:
            logger.debug("EngineCore interrupted.")

        except Exception:
            traceback = get_exception_traceback()
            logger.error("EngineCore hit an exception: %s", traceback)
            parent_process.send_signal(signal.SIGUSR1)

        finally:
            if engine_core is not None:
                engine_core.shutdown()

    def run_busy_loop(self):
        """Core busy loop of the EngineCore."""

        step_fn = (self.step
                   if self.batch_queue is None else self.step_with_batch_queue)

        # Loop until process is sent a SIGINT or SIGTERM
        while True:
            # 1) Poll the input queue until there is work to do.
            while not self.scheduler.has_requests():
                logger.debug("EngineCore busy loop waiting.")
                req = self.input_queue.get()
                self._handle_client_request(*req)

            # 2) Handle any new client requests.
            while not self.input_queue.empty():
                req = self.input_queue.get_nowait()
                self._handle_client_request(*req)

            # 3) Step the engine core.
            outputs = step_fn()

            # 4) Put EngineCoreOutputs into the output queue.
            if outputs is not None:
                self.output_queue.put_nowait(outputs)

    def _handle_client_request(self, request_type: EngineCoreRequestType,
                               request: Any) -> None:
        """Dispatch request from client."""

        if request_type == EngineCoreRequestType.ADD:
            self.add_request(request)
        elif request_type == EngineCoreRequestType.ABORT:
            self.abort_requests(request)
        elif request_type == EngineCoreRequestType.UTILITY:
            call_id, method_name, args = request
            output = UtilityOutput(call_id)
            try:
                method = getattr(self, method_name)
                output.result = method(
                    *self._convert_msgspec_args(method, args))
            except BaseException as e:
                logger.exception("Invocation of %s method failed", method_name)
                output.failure_message = (f"Call to {method_name} method"
                                          f" failed: {str(e)}")
            self.output_queue.put_nowait(
                EngineCoreOutputs(utility_output=output))

    @staticmethod
    def _convert_msgspec_args(method, args):
        """If a provided arg type doesn't match corresponding target method
         arg type, try converting to msgspec object."""
        if not args:
            return args
        arg_types = signature(method).parameters.values()
        assert len(args) <= len(arg_types)
        return tuple(
            msgspec.convert(v, type=p.annotation) if isclass(p.annotation)
            and issubclass(p.annotation, msgspec.Struct)
            and not isinstance(v, p.annotation) else v
            for v, p in zip(args, arg_types))

    def process_input_socket(self, input_path: str):
        """Input socket IO thread."""

        # Msgpack serialization decoding.
        add_request_decoder = MsgpackDecoder(EngineCoreRequest)
        generic_decoder = MsgpackDecoder()

        with zmq_socket_ctx(input_path, zmq.constants.PULL) as socket:
            while True:
                # (RequestType, RequestData)
                type_frame, data_frame = socket.recv_multipart(copy=False)
                request_type = EngineCoreRequestType(bytes(type_frame.buffer))

                # Deserialize the request data.
                decoder = add_request_decoder if (
                    request_type
                    == EngineCoreRequestType.ADD) else generic_decoder
                request = decoder.decode(data_frame.buffer)

                # Push to input queue for core busy loop.
                self.input_queue.put_nowait((request_type, request))

    def process_output_socket(self, output_path: str):
        """Output socket IO thread."""

        # Msgpack serialization encoding.
        encoder = MsgpackEncoder()
        # Reuse send buffer.
        buffer = bytearray()

        with zmq_socket_ctx(output_path, zmq.constants.PUSH) as socket:
            while True:
                outputs = self.output_queue.get()
                encoder.encode_into(outputs, buffer)
                socket.send_multipart((buffer, ), copy=False)
