import os
import sys
import uuid
import time
import socket
import gradio as gr
import importlib.util
import shutil

from gradio.context import Context
from threading import Thread
from huggingface_hub import snapshot_download
from backend import memory_management


spaces = []


def build_html(title, installed=False, url=None):
    if not installed:
        return f'<div>{title}</div><div style="color: grey;">Not Installed</div>'

    if isinstance(url, str):
        return f'<div>{title}</div><div style="color: green;">Currently Running: <a href="{url}" style="color: blue;" target="_blank">{url}</a></div>'
    else:
        return f'<div>{title}</div><div style="color: grey;">Installed, Ready to Launch</div>'


def find_free_port(server_name, start_port=None):
    port = start_port

    if port is None:
        port = 7860

    if server_name is None:
        server_name = '127.0.0.1'

    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((server_name, port))
                return port
            except OSError:
                port += 1


class ForgeSpace:
    def __init__(self, root_path, title, repo_id=None, repo_type='space', revision=None, **kwargs):
        self.title = title
        self.root_path = root_path
        self.hf_path = os.path.join(root_path, 'huggingface_space_mirror')
        self.repo_id = repo_id
        self.repo_type = repo_type
        self.revision = revision
        self.is_running = False
        self.gradio_metas = None

        self.label = gr.HTML(build_html(title=title, url=None), elem_classes=['forge_space_label'])
        self.btn_launch = gr.Button('Launch', elem_classes=['forge_space_btn'])
        self.btn_terminate = gr.Button('Terminate', elem_classes=['forge_space_btn'])
        self.btn_install = gr.Button('Install', elem_classes=['forge_space_btn'])
        self.btn_uninstall = gr.Button('Uninstall', elem_classes=['forge_space_btn'])

        comps = [
            self.label,
            self.btn_install,
            self.btn_uninstall,
            self.btn_launch,
            self.btn_terminate
        ]

        self.btn_launch.click(self.run, outputs=comps)
        self.btn_terminate.click(self.terminate, outputs=comps)
        self.btn_install.click(self.install, outputs=comps)
        self.btn_uninstall.click(self.uninstall, outputs=comps)
        Context.root_block.load(self.refresh_gradio, outputs=comps, queue=False, show_progress=False)

        return

    def refresh_gradio(self):
        results = []

        installed = os.path.exists(self.hf_path)

        if isinstance(self.gradio_metas, tuple):
            results.append(build_html(title=self.title, installed=installed, url=self.gradio_metas[1]))
        else:
            results.append(build_html(title=self.title, installed=installed, url=None))

        results.append(gr.update(interactive=not self.is_running and not installed))
        results.append(gr.update(interactive=not self.is_running and installed))
        results.append(gr.update(interactive=installed and not self.is_running))
        results.append(gr.update(interactive=installed and self.is_running))
        return results

    def install(self):
        os.makedirs(self.hf_path, exist_ok=True)

        if self.repo_id is None:
            return self.refresh_gradio()

        downloaded = snapshot_download(
            repo_id=self.repo_id,
            repo_type=self.repo_type,
            revision=self.revision,
            local_dir=self.hf_path,
            force_download=True,
        )

        print(f'Downloaded: {downloaded}')

        requirements_filename = os.path.abspath(os.path.realpath(os.path.join(self.root_path, 'requirements.txt')))

        if os.path.exists(requirements_filename):
            from modules.launch_utils import run_pip
            run_pip(f'install -r "{requirements_filename}"', desc=f"space requirements for [{self.title}]")

        return self.refresh_gradio()

    def uninstall(self):
        def on_rm_error(func, path, exc_info):
            print(f"Error deleting {path}. Error: {exc_info[1]}")
            print(f'Something went wrong when trying to delete a folder. You may try to manually delete the folder [{self.hf_path}].')

        shutil.rmtree(self.hf_path, onerror=on_rm_error)
        print(f'Deleted: {self.hf_path}')
        return self.refresh_gradio()

    def terminate(self):
        self.is_running = False
        while self.gradio_metas is not None:
            time.sleep(0.1)
        return self.refresh_gradio()

    def run(self):
        self.is_running = True
        Thread(target=self.gradio_worker).start()
        while self.gradio_metas is None:
            time.sleep(0.1)
        return self.refresh_gradio()

    def gradio_worker(self):
        import spaces
        spaces.unload_module()
        original_cwd = os.getcwd()
        os.chdir(self.hf_path)

        if 'models' in sys.modules:
            del sys.modules['models']

        memory_management.unload_all_models()
        sys.path.insert(0, self.hf_path)
        sys.path.insert(0, self.root_path)
        file_path = os.path.join(self.root_path, 'forge_app.py')
        module_name = 'forge_space_' + str(uuid.uuid4()).replace('-', '_')
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        demo = getattr(module, 'demo')

        from modules import initialize_util
        from modules.shared import cmd_opts

        server_name = initialize_util.gradio_server_name()
        port = find_free_port(server_name=server_name, start_port=cmd_opts.port)

        self.gradio_metas = demo.launch(
            inbrowser=True,
            prevent_thread_lock=True,
            server_name=server_name,
            server_port=port
        )

        if module_name in sys.modules:
            del sys.modules[module_name]

        if 'models' in sys.modules:
            del sys.modules['models']

        sys.path.remove(self.hf_path)
        sys.path.remove(self.root_path)
        os.chdir(original_cwd)

        while self.is_running:
            time.sleep(0.1)

        demo.close()
        self.gradio_metas = None
        return


def main_entry():
    global spaces

    from modules.extensions import extensions

    tagged_extensions = {}

    for ex in extensions:
        if ex.enabled and ex.is_forge_space:
            tag = ex.space_meta['tag']

            if tag not in tagged_extensions:
                tagged_extensions[tag] = []

            tagged_extensions[tag].append(ex)

    for tag, exs in tagged_extensions.items():
        with gr.Accordion(tag, open=True):
            for ex in exs:
                with gr.Row(equal_height=True):
                    space = ForgeSpace(root_path=ex.path, **ex.space_meta)
                    spaces.append(space)

    return