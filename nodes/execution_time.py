import time
import torch
import inspect
import execution
import server
import logging

logger = logging.getLogger("ComfyUI")

class ExecutionTime:
    CATEGORY = "TyDev-Utils/Debug"

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {}}

    RETURN_TYPES = ()
    RETURN_NAMES = ()
    FUNCTION = "process"

    def process(self):
        return ()

CURRENT_START_EXECUTION_DATA = None

def get_peak_memory():
    if not torch.cuda.is_available():
        return 0
    device = torch.device('cuda')
    return torch.cuda.max_memory_allocated(device)

def reset_peak_memory_record():
    if not torch.cuda.is_available():
        return
    device = torch.device('cuda')
    torch.cuda.reset_max_memory_allocated(device)

def ensure_execution_data():
    global CURRENT_START_EXECUTION_DATA
    if CURRENT_START_EXECUTION_DATA is None:
        logger.info("[ExecutionTime] Initializing execution data")
        CURRENT_START_EXECUTION_DATA = dict(
            start_perf_time=time.perf_counter(),
            nodes_start_perf_time={},
            nodes_start_vram={}
        )

def handle_execute(class_type, last_node_id, prompt_id, server, unique_id):
    ensure_execution_data()
    
    # 记录节点开始时间和VRAM
    if unique_id not in CURRENT_START_EXECUTION_DATA['nodes_start_perf_time']:
        CURRENT_START_EXECUTION_DATA['nodes_start_perf_time'][unique_id] = time.perf_counter()
        reset_peak_memory_record()
        CURRENT_START_EXECUTION_DATA['nodes_start_vram'][unique_id] = get_peak_memory()
        logger.info(f"[ExecutionTime] Node #{unique_id} [{class_type}] execution started")
        return
    
    start_time = CURRENT_START_EXECUTION_DATA['nodes_start_perf_time'].get(unique_id)
    start_vram = CURRENT_START_EXECUTION_DATA['nodes_start_vram'].get(unique_id)
    
    end_time = time.perf_counter()
    execution_time = end_time - start_time

    end_vram = get_peak_memory()
    vram_used = end_vram - start_vram
    
    # 始终输出日志，不管是API还是Web UI调用
    logger.info(f"Node Execution Time - #{unique_id} [{class_type}]: {execution_time:.2f}s - VRAM Used: {vram_used/1024/1024:.2f}MB")
    
    # 如果是Web UI调用，还发送WebSocket消息
    if server.client_id is not None and last_node_id != server.last_node_id:
        try:
            server.send_sync(
                "TyDev-Utils.ExecutionTime.executed",
                {"node": unique_id, "prompt_id": prompt_id, "execution_time": int(execution_time * 1000),
                 "vram_used": vram_used},
                server.client_id
            )
        except Exception as e:
            logger.info(f"[ExecutionTime] Failed to send WebSocket message: {e}")

    # 清除已完成节点的数据
    del CURRENT_START_EXECUTION_DATA['nodes_start_perf_time'][unique_id]
    del CURRENT_START_EXECUTION_DATA['nodes_start_vram'][unique_id]

# 尝试注入执行时间统计代码
try:
    logger.info("[ExecutionTime] Attempting to inject execution time monitoring...")
    
    origin_execute = execution.execute
    
    if inspect.iscoroutinefunction(origin_execute):
        async def dev_utils_execute(server, dynprompt, caches, current_item, extra_data, executed, prompt_id,
                                    execution_list, pending_subgraph_results, pending_async_nodes):
            unique_id = current_item
            class_type = dynprompt.get_node(unique_id)['class_type']
            last_node_id = server.last_node_id
            
            # 记录开始时间
            handle_execute(class_type, last_node_id, prompt_id, server, unique_id)
            
            result = await origin_execute(server, dynprompt, caches, current_item, extra_data, executed, prompt_id,
                                          execution_list, pending_subgraph_results, pending_async_nodes)
            
            # 记录结束时间
            handle_execute(class_type, last_node_id, prompt_id, server, unique_id)
            return result
    else:
        def dev_utils_execute(server, dynprompt, caches, current_item, extra_data, executed, prompt_id,
                              execution_list, pending_subgraph_results):
            unique_id = current_item
            class_type = dynprompt.get_node(unique_id)['class_type']
            last_node_id = server.last_node_id
            
            # 记录开始时间
            handle_execute(class_type, last_node_id, prompt_id, server, unique_id)
            
            result = origin_execute(server, dynprompt, caches, current_item, extra_data, executed, prompt_id,
                                    execution_list, pending_subgraph_results)
            
            # 记录结束时间
            handle_execute(class_type, last_node_id, prompt_id, server, unique_id)
            return result

    execution.execute = dev_utils_execute
    logger.info("[ExecutionTime] Successfully injected execution time monitoring")
except Exception as e:
    logger.error(f"[ExecutionTime] Failed to inject execution time monitoring: {e}")

# 处理 WebSocket 事件
origin_func = server.PromptServer.send_sync

def dev_utils_send_sync(self, event, data, sid=None):
    global CURRENT_START_EXECUTION_DATA
    
    if event == "execution_start":
        logger.info("[ExecutionTime] Execution started")
        CURRENT_START_EXECUTION_DATA = dict(
            start_perf_time=time.perf_counter(),
            nodes_start_perf_time={},
            nodes_start_vram={}
        )
    elif event == "execution_cached" or event == "execution_error":
        logger.info(f"[ExecutionTime] Execution {event}")
        CURRENT_START_EXECUTION_DATA = None

    origin_func(self, event=event, data=data, sid=sid)

    if event == "executing" and data and CURRENT_START_EXECUTION_DATA:
        if data.get("node") is None and sid is not None:
            start_perf_time = CURRENT_START_EXECUTION_DATA.get('start_perf_time')
            if start_perf_time is not None:
                execution_time = time.perf_counter() - start_perf_time
                logger.info(f"[ExecutionTime] Total execution time: {execution_time:.2f}s")
                try:
                    new_data = data.copy()
                    new_data['execution_time'] = int(execution_time * 1000)
                    origin_func(
                        self,
                        event="TyDev-Utils.ExecutionTime.execution_end",
                        data=new_data,
                        sid=sid
                    )
                except Exception as e:
                    logger.info(f"[ExecutionTime] Failed to send final WebSocket message: {e}")
            CURRENT_START_EXECUTION_DATA = None

server.PromptServer.send_sync = dev_utils_send_sync