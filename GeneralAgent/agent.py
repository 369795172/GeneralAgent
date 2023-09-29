# Agent

import os, re
import asyncio
import logging
import datetime
import platform
from jinja2 import Template
from GeneralAgent.prompts import general_agent_prompt
from GeneralAgent.llm import llm_inference
from GeneralAgent.tools import Tools
from GeneralAgent.memory import Memory, MemoryNode
from GeneralAgent.interpreter import PlanInterpreter
from GeneralAgent.interpreter import PythonInterpreter, FileInterpreter, BashInterperter, AppleScriptInterpreter, AskInterpreter

def default_output_recall(output):
    if output is not None:
        print(output, end='', flush=True)
    else:
        print('\n[output end]\n', end='', flush=True)


class Agent:
    def __init__(self, workspace, tools=None, max_plan_depth=4):
        self.workspace = workspace
        if not os.path.exists(workspace):
            os.makedirs(workspace)
        self.memory = Memory(serialize_path=f'{workspace}/memory.json')
        self.tools = tools or Tools([])
        self.is_running = False
        self.stop_event = asyncio.Event()
        self.os_version = get_os_version()

        # input interpreters
        self.plan_interperter = PlanInterpreter(self.memory, max_plan_depth)
        self.input_interpreters = [self.plan_interperter]

        # output interpreters
        self.python_interpreter = PythonInterpreter(serialize_path=f'{workspace}/code.bin')
        self.bash_interpreter = BashInterperter('./')
        self.applescript_interpreter = AppleScriptInterpreter()
        self.file_interpreter = FileInterpreter('./')
        self.ask_interpreter = AskInterpreter()
        self.output_interpreters = [self.python_interpreter, self.bash_interpreter, self.applescript_interpreter, self.file_interpreter, self.ask_interpreter]

    async def run(self, input=None, for_node_id=None, output_recall=default_output_recall):
        self.is_running = True
        input_node = self._insert_node(input, for_node_id) if input is not None else None

        # input interpreter
        if input_node is not None:
            self.memory.set_current_node(input_node)
            for interpreter in self.input_interpreters:
                match = re.compile(interpreter.match_template, re.DOTALL).search(input_node.content)
                if match is not None:
                    logging.info('interpreter: ' + interpreter.__class__.__name__)
                    interpreter.parse(input_node.content)
                    break

        # execute todo node from memory
        todo_node = self.memory.get_todo_node() or input_node
        logging.debug(self.memory)
        while todo_node is not None:
            new_node, is_stop = await self._execute_node(todo_node, output_recall)
            logging.debug(self.memory)
            logging.debug(new_node)
            logging.debug(is_stop)
            if is_stop:
                return new_node.node_id
            todo_node = self.memory.get_todo_node()
            await asyncio.sleep(0)
            if self.stop_event.is_set():
                self.is_running = False
                return None
        self.is_running = False
        return None
    
    def stop(self):
        self.stop_event.set()

    def _insert_node(self, input, for_node_id=None):
        node = MemoryNode(role='user', action='input', content=input)
        if for_node_id is None:
            self.memory.add_node(node)
        else:
            for_node = self.memory.get_node(for_node_id)
            self.memory.add_node_after(for_node, node)
            self.memory.success_node(for_node)
        return node
    
    async def _execute_node(self, node, output_recall):
        python_libs = ', '.join([line.strip() for line in open(os.path.join(os.path.dirname(__file__), '../requirements.txt'), 'r').readlines()])
        python_funcs = self.tools.get_funs_description()

        # set time same all the time if cache is on
        if os.environ.get('LLM_CACHE', 'no') in ['yes', 'y', 'YES']:
            now = '2023-09-27 00:00:00'
        else:    
            now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        system_variables = {
            'now': now,
            'os_version': self.os_version,
            'python_libs': python_libs,
            'python_funcs': python_funcs
        }
        system_prompt = Template(general_agent_prompt).render(**system_variables)
        messages = [{'role': 'system', 'content': system_prompt}] + self.memory.get_related_messages_for_node(node)

        # add answer node and set current node
        answer_node = MemoryNode(role='system', action='answer', content='')
        self.memory.add_node_after(node, answer_node)
        self.memory.set_current_node(answer_node)

        if node.action == 'plan':
            await output_recall(f'\n[{node.content}]\n')

        # TODO: when messages exceed limit, cut it

        try:
            result = ''
            is_stop = False
            is_break = False
            response = llm_inference(messages)
            for token in response:
                if token is None: break
                result += token
                await output_recall(token)
                for interpreter in self.output_interpreters:
                    match = re.compile(interpreter.match_template, re.DOTALL).search(result)
                    if match is not None:
                        logging.info('interpreter: ' + interpreter.__class__.__name__)
                        output, is_stop = interpreter.parse(result)
                        await output_recall('\n' + output + '\n')
                        is_break = True
                        break
                if is_break:
                    break
            await output_recall(None)
            # update current node and answer node
            answer_node.content = result
            self.memory.success_node(node)
            self.memory.success_node(answer_node)
            return answer_node, is_stop
        except Exception as e:
            # if fail, recover
            logging.exception(e)
            await output_recall(result)
            self.memory.delete_node(answer_node)
            return node, is_stop


def get_os_version():
    system = platform.system()
    if system == 'Windows':
        version = platform.version()
        return f"Windows version: {version}"
    elif system == 'Darwin':
        version = platform.mac_ver()[0]
        return f"macOS version: {version}"
    elif system == 'Linux':
        dist = platform.linux_distribution()
        if dist[0] == 'CentOS':
            version = dist[1]
            return f"CentOS version: {version}"
        elif dist[0] == 'Ubuntu':
            version = dist[1]
            return f"Ubuntu version: {version}"
    else:
        return "Unknown system"
