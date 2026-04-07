import gradio as gr
import os, subprocess, re, requests, shutil, json, glob, time
from urllib.parse import urlparse

# Ensure environment paths map perfectly to our Docker/RunPod container
WORKSPACE_ROOT = "/workspace"
COMFY_ROOT = os.path.join(WORKSPACE_ROOT, "ComfyUI")
COMFY_OUTPUT = os.path.join(COMFY_ROOT, "output")
HISTORY_FILE = os.path.join(WORKSPACE_ROOT, "sidecar_history.json")
TOKENS_FILE = os.path.join(WORKSPACE_ROOT, "tokens.txt")
VENV_PYTHON = os.path.join(WORKSPACE_ROOT, "venv/bin/python")
VENV_PIP = os.path.join(WORKSPACE_ROOT, "venv/bin/uv")

os.makedirs(COMFY_OUTPUT, exist_ok=True)
os.makedirs("/workspace/logs", exist_ok=True)
os.makedirs("/workspace/openwebui_data", exist_ok=True)

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================
def get_tokens():
    tokens = {"HF": os.environ.get("HF_TOKEN"), "CIVITAI": os.environ.get("CIVITAI_TOKEN")}
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("HF_TOKEN=") and not tokens["HF"]: tokens["HF"] = line.split("=", 1)[1].strip()
                elif line.startswith("CIVITAI_TOKEN=") and not tokens["CIVITAI"]: tokens["CIVITAI"] = line.split("=", 1)[1].strip()
    return tokens

def format_bytes(size_in_bytes):
    if size_in_bytes < 1024**2: return f"{size_in_bytes / 1024:.1f} KB"
    elif size_in_bytes < 1024**3: return f"{size_in_bytes / (1024**2):.1f} MB"
    else: return f"{size_in_bytes / (1024**3):.2f} GB"

def get_dir_size(start_path):
    total_size = 0
    for dirpath, _, filenames in os.walk(start_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp) and os.path.exists(fp): total_size += os.path.getsize(fp)
    return total_size

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f: return json.load(f)
        except: pass
    return[]

def save_history(history_list):
    with open(HISTORY_FILE, "w") as f: json.dump(history_list, f, indent=4)

def append_history(name, path, is_node, size_str):
    hist = load_history()
    hist =[h for h in hist if h['path'] != path]
    hist.append({"name": name, "path": path, "is_node": is_node, "size": size_str})
    save_history(hist)

# ==============================================================================
# TAB 1: DOWNLOADER & SYNC LOGIC
# ==============================================================================
current_process = None
cancel_requested = False

def request_cancel():
    global cancel_requested, current_process
    cancel_requested = True
    if current_process:
        try: current_process.kill()
        except: pass
    return "⚠️ Cancellation triggered! Killing active network processes..."

def sync_generator(file_path):
    global cancel_requested, current_process
    cancel_requested = False
    current_process = None
    auth_tokens = get_tokens()

    if not file_path:
        yield "❌ Error: No file uploaded.", "No queue", gr.update()
        return

    if hasattr(file_path, "name"): file_path = file_path.name

    with open(file_path, "r") as f: lines = f.readlines()

    tasks =[]
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"): continue
        
        url_match = re.match(r"^(https?://\S+)", line)
        if url_match:
            url = url_match.group(1)
            tag_match = re.search(r"\[([^'\]]+)\]", line[len(url):])
            name_match = re.search(r"\['([^']+)'\]", line[len(url):])
            
            tag = tag_match.group(1).strip() if tag_match else "models/checkpoints"
            custom_name = name_match.group(1).strip() if name_match else None

            if "github.com" in url or url.endswith(".git"):
                url = url if url.endswith(".git") else url + ".git"
                tasks.append({"url": url, "tag": "custom_nodes", "custom_name": None, "status": "pending", "size": "", "path": ""})
            else:
                tasks.append({"url": url, "tag": tag, "custom_name": custom_name, "status": "pending", "size": "", "path": ""})

    def render_queue(current_idx=-1):
        q_lines =[]
        for i, t in enumerate(tasks):
            display_name = t['custom_name'] if t['custom_name'] else t['url'].rstrip('/').split('/')[-1].replace('.git', '')
            icon = "⏳"
            if t['status'] == "done": 
                icon = "✅"
                display_name += f"  💾 ({t['size']})"
            elif t['status'] == "error": icon = "❌"
            elif t['status'] == "cancelled": icon = "🛑"
            elif i == current_idx: icon = "▶️"
            q_lines.append(f"{icon} {i+1}. {display_name}")
        return "\n".join(q_lines)

    log_history =[]
    def update_log(msg, replace_last=False):
        if replace_last and log_history and log_history[-1].startswith("   ->"):
            log_history[-1] = f"   -> {msg}"
        else:
            log_history.append(f"   -> {msg}" if replace_last else msg)
        return "\n".join(log_history[-20:])

    if not tasks:
        yield update_log("⚠️ No valid tasks found in text file."), "Empty", gr.update(value=None)
        return

    current_queue_ui = render_queue()
    yield update_log(f"🔍 Found {len(tasks)} tasks. Starting Queue..."), current_queue_ui, gr.update()

    for i, task in enumerate(tasks):
        if cancel_requested: break

        url, tag, custom_name = task["url"], task["tag"], task["custom_name"]
        current_queue_ui = render_queue(i)
        yield update_log(f"\n--- Task {i+1} of {len(tasks)} ---"), current_queue_ui, gr.update()

        if "custom_nodes" in tag or url.endswith(".git"):
            repo_name = url.rstrip('/').split('/')[-1].replace(".git", "")
            target_dir = os.path.join(COMFY_ROOT, "custom_nodes", repo_name)
            
            if not os.path.exists(target_dir):
                yield update_log(f"📦 Cloning Node: {repo_name}..."), current_queue_ui, gr.update()
                try:
                    current_process = subprocess.Popen(["git", "clone", "--depth", "1", url, target_dir], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    current_process.wait()
                    if cancel_requested: 
                        tasks[i]["status"] = "cancelled"
                        continue
                    if current_process.returncode != 0: raise Exception("Git clone failed.")
                    
                    req_file = os.path.join(target_dir, "requirements.txt")
                    if os.path.exists(req_file):
                        yield update_log(f"⚙️ Installing dependencies via uv..."), current_queue_ui, gr.update()
                        current_process = subprocess.Popen([VENV_PIP, "pip", "install", "--python", VENV_PYTHON, "-r", req_file], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                        current_process.wait()

                    tasks[i]["status"] = "done"
                    folder_size = format_bytes(get_dir_size(target_dir))
                    tasks[i]["size"] = folder_size
                    append_history(repo_name, target_dir, True, folder_size)
                    
                    yield update_log(f"✅ Finished Node: {repo_name}"), render_queue(i), gr.update()
                except Exception as e:
                    tasks[i]["status"] = "error"
                    yield update_log(f"❌ Error: {str(e)}"), render_queue(i), gr.update()
                finally:
                    current_process = None
            else:
                tasks[i]["status"] = "done"
                tasks[i]["size"] = format_bytes(get_dir_size(target_dir))
                yield update_log(f"ℹ️ Node {repo_name} already exists. Skipping."), render_queue(i), gr.update()

        else:
            dest_dir = os.path.join(COMFY_ROOT, tag)
            os.makedirs(dest_dir, exist_ok=True)
            file_name = custom_name if custom_name else os.path.basename(urlparse(url).path).split("?")[0]
            dest_file = os.path.join(dest_dir, file_name)
            
            yield update_log(f"⏳ Downloading Model: {file_name}..."), current_queue_ui, gr.update()
            
            cmd =["aria2c", "--allow-overwrite=true", "--auto-file-renaming=false", "-x", "16", "-s", "16", "--console-log-level=warn", "--summary-interval=1"]
            if "huggingface.co" in url and auth_tokens["HF"]: cmd.append(f"--header=Authorization: Bearer {auth_tokens['HF']}")
            cmd.extend(["-d", dest_dir, "-o", file_name, url])
            
            try:
                current_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for output in current_process.stdout:
                    if cancel_requested:
                        current_process.kill()
                        break
                    output = output.strip()
                    if not output: continue
                    m = re.search(r"([\d\.]+[KMG]?iB)/([\d\.]+[KMG]?iB)\((\d+)%\)", output)
                    if m:
                        dl, total, pct = m.groups()
                        yield update_log(f"[{pct}% | {dl} / {total}]", replace_last=True), render_queue(i), gr.update()
                        
                current_process.wait()
                if cancel_requested:
                    tasks[i]["status"] = "cancelled"
                    continue
                
                if current_process.returncode == 0:
                    tasks[i]["status"] = "done"
                    tasks[i]["size"] = format_bytes(os.path.getsize(dest_file))
                    append_history(file_name, dest_file, False, tasks[i]["size"])
                    yield update_log("✅ Complete", replace_last=True), render_queue(i), gr.update()
                else:
                    tasks[i]["status"] = "error"
                    yield update_log(f"❌ Aria2 Failed (Code {current_process.returncode})"), render_queue(i), gr.update()
            except Exception as e:
                tasks[i]["status"] = "error"
                yield update_log(f"❌ Error: {str(e)}"), render_queue(i), gr.update()
            finally:
                current_process = None

    if cancel_requested:
        yield update_log("\n🛑 PROCESS CANCELLED."), render_queue(), gr.update(value=None)
        return

    yield update_log("\n🔄 Refreshing ComfyUI Nodes..."), render_queue(), gr.update()
    try:
        requests.post("http://127.0.0.1:8188/api/refresh", timeout=5)
        yield update_log("🚀 ALL TASKS COMPLETE. ComfyUI nodes refreshed!"), render_queue(), gr.update(value=None)
    except:
        yield update_log("⚠️ Done, but ComfyUI is not responding to API refresh."), render_queue(), gr.update(value=None)


# ==============================================================================
# TAB 2: DATASET AUTO-TAGGER (FLORENCE-2)
# ==============================================================================
def run_florence_tagger(folder_path, prompt):
    if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
        yield "❌ Error: Invalid directory path."
        return

    yield f"🔥 Loading Florence-2 Large into VRAM. This takes a few seconds..."
    try:
        import torch
        from transformers import AutoProcessor, AutoModelForCausalLM
        from PIL import Image
        import gc
    except ImportError:
        yield "❌ Transformers not found. Please restart container."
        return

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_id = "microsoft/Florence-2-large"

    try:
        model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=torch.float16, trust_remote_code=True).to(device)
        processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    except Exception as e:
        yield f"❌ Failed to load model: {str(e)}"
        return

    images =[]
    for ext in ["*.png", "*.jpg", "*.jpeg", "*.webp"]:
        images.extend(glob.glob(os.path.join(folder_path, ext)))
        
    if not images:
        yield "⚠️ No images found in the directory!"
        return
        
    yield f"🔍 Found {len(images)} images. Starting captioning...\n"
    
    success_count = 0
    for i, img_path in enumerate(images):
        try:
            image = Image.open(img_path).convert("RGB")
            inputs = processor(text=prompt, images=image, return_tensors="pt").to(device, torch.float16)
            generated_ids = model.generate(input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"], max_new_tokens=1024, do_sample=False, num_beams=3)
            generated_text = processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
            parsed_answer = processor.post_process_generation(generated_text, task=prompt, image_size=(image.width, image.height))
            
            caption = parsed_answer.get(prompt, generated_text)
            txt_path = os.path.splitext(img_path)[0] + ".txt"
            
            with open(txt_path, "w", encoding="utf-8") as f: f.write(caption)
                
            success_count += 1
            yield f"✅[{i+1}/{len(images)}] Tagged: {os.path.basename(img_path)}\n   -> {caption[:50]}..."
        except Exception as e:
            yield f"❌ [{i+1}/{len(images)}] Failed on {os.path.basename(img_path)}: {str(e)}"

    del model
    del processor
    gc.collect()
    torch.cuda.empty_cache()
    yield f"\n🎉 Finished! Successfully tagged {success_count} / {len(images)} images.\n🧹 VRAM Cleared!"

# ==============================================================================
# TAB 3: GATEWAY DASHBOARD
# ==============================================================================
def get_gateway_links():
    pod_id = os.environ.get("RUNPOD_POD_ID")
    apps = {
        "ComfyUI": ("8188", "🎨"),
        "Filebrowser": ("8083", "📂"), 
        "Kohya_ss": ("7860", "🏋️"),
        "Open-WebUI": ("8082", "💬"),
        "Langflow": ("3000", "⛓️"),
        "VS Code": ("8888", "💻")
    }
    
    html = "<div style='display: flex; gap: 15px; flex-wrap: wrap; margin-top: 10px;'>"
    for app, (port, icon) in apps.items():
        if pod_id:
            url = f"https://{pod_id}-{port}.proxy.runpod.net"
        else:
            url = f"http://localhost:{port}"
        
        html += f"""
        <a href="{url}" target="_blank" style="
            display: flex; align-items: center; justify-content: center; gap: 10px;
            padding: 20px 30px; background-color: #2b2d31; color: white; 
            text-decoration: none; border-radius: 12px; font-weight: bold; 
            font-size: 18px; border: 2px solid #5865f2; transition: 0.2s;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1); flex-grow: 1; text-align: center;
        ">
            <span style="font-size: 24px;">{icon}</span> Open {app}
        </a>
        """
    html += "</div>"
    return html

# ==============================================================================
# TAB 4: THE APP STORE
# ==============================================================================
bg_processes = {}

def run_cmd_with_logs(cmd, cwd=None, env=None):
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd, env=env)
    for line in iter(process.stdout.readline, ''): yield line
    process.wait()
    if process.returncode != 0: yield f"\n❌ Process failed with exit code {process.returncode}\n"

def app_store_action(app_name, action):
    global bg_processes
    log_output = f"--- Executing {action} for {app_name} ---\n"
    yield log_output
    
    if action == "Stop":
        if app_name in bg_processes and bg_processes[app_name].poll() is None:
            bg_processes[app_name].terminate()
            del bg_processes[app_name]
            yield log_output + f"🛑 Stopped {app_name}."
        else:
            yield log_output + f"ℹ️ {app_name} is not running."
        return

    if app_name in bg_processes and bg_processes[app_name].poll() is None:
        yield log_output + f"✅ {app_name} is already running."
        return

    env = os.environ.copy()
    env["PATH"] = f"/root/.local/bin:/workspace/venv/bin:{env.get('PATH', '')}"

    try:
        # --- ADD THIS SELF-HEALING BLOCK ---
        if not os.path.exists(VENV_PIP):
            log_output += "📦 Restoring 'uv' package manager...\n"; yield log_output
            for line in run_cmd_with_logs([VENV_PYTHON, "-m", "pip", "install", "uv"]): log_output += line; yield log_output
        # -----------------------------------

        if app_name == "ComfyUI":
            if not os.path.exists(COMFY_ROOT):
                log_output += "📦 Cloning ComfyUI...\n"; yield log_output
                for line in run_cmd_with_logs(["git", "clone", "https://github.com/comfyanonymous/ComfyUI.git", COMFY_ROOT]): log_output += line; yield log_output
                
                # We strip PyTorch because entrypoint.sh already installed the perfect GPU-matched version
                log_output += "\n🧹 Stripping base PyTorch from requirements...\n"; yield log_output
                subprocess.run(["sed", "-i", "-E", "/^(torch|torchvision|torchaudio|xformers)/Id", f"{COMFY_ROOT}/requirements.txt"])
                
                log_output += "\n⚙️ Installing ComfyUI dependencies via uv...\n"; yield log_output
                for line in run_cmd_with_logs([VENV_PIP, "pip", "install", "--python", VENV_PYTHON, "-r", f"{COMFY_ROOT}/requirements.txt"]): log_output += line; yield log_output

            # Memory Optimization Environment Variables
            env["CUDA_MODULE_LOADING"] = "LAZY"
            env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
            env["TORCH_CUDNN_V8_API_ENABLED"] = "1"

            # Check if SageAttention was successfully compiled by the bootloader
            attn_args =[]
            try:
                subprocess.run([VENV_PYTHON, "-c", "import sageattention"], check=True, capture_output=True)
                attn_args.append("--use-sage-attention")
                log_output += "✨ SageAttention detected and enabled!\n"; yield log_output
            except:
                pass

            log_output += "\n🚀 Starting ComfyUI on port 8188...\n"; yield log_output
            log_file = open("/workspace/logs/comfyui.log", "w")
            
            # Added --force-fp16 and --fast flags from your script
            cmd =[VENV_PYTHON, "main.py", "--listen", "0.0.0.0", "--port", "8188", "--force-fp16", "--fast"] + attn_args
            bg_processes[app_name] = subprocess.Popen(cmd, cwd=COMFY_ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT)
            
            yield log_output + "✅ ComfyUI is running! (Port 8188)\n"

        elif app_name == "Kohya_ss":
            kohya_dir = os.path.join(WORKSPACE_ROOT, "kohya_ss")
            if not os.path.exists(kohya_dir):
                log_output += "📦 Cloning Kohya_ss...\n"; yield log_output
                for line in run_cmd_with_logs(["git", "clone", "--recursive", "https://github.com/bmaltais/kohya_ss.git", kohya_dir]): log_output += line; yield log_output
                log_output += "\n⚙️ Installing Kohya dependencies via uv...\n"; yield log_output
                req_file = f"{kohya_dir}/requirements_linux.txt" if os.path.exists(f"{kohya_dir}/requirements_linux.txt") else f"{kohya_dir}/requirements.txt"
                for line in run_cmd_with_logs([VENV_PIP, "pip", "install", "--python", VENV_PYTHON, "-r", req_file]): log_output += line; yield log_output

            gui_script = "gui.py" if os.path.exists(f"{kohya_dir}/gui.py") else "kohya_gui.py"
            log_output += "\n🚀 Starting Kohya_ss on port 7860...\n"; yield log_output
            log_file = open("/workspace/logs/kohya.log", "w")
            bg_processes[app_name] = subprocess.Popen([VENV_PYTHON, gui_script, "--listen", "0.0.0.0", "--server_port", "7860"], cwd=kohya_dir, stdout=log_file, stderr=subprocess.STDOUT)
            yield log_output + "✅ Kohya_ss is running! (Port 7860)\n"

        elif app_name == "Open-WebUI":
            log_output += "📦 Ensuring Open-WebUI is installed...\n"; yield log_output
            for line in run_cmd_with_logs([VENV_PIP, "pip", "install", "--python", VENV_PYTHON, "open-webui"]): log_output += line; yield log_output
            env["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
            env["WEBUI_AUTH"] = "False"
            env["DATA_DIR"] = "/workspace/openwebui_data"
            log_output += "\n🚀 Starting Open-WebUI on port 8082...\n"; yield log_output
            log_file = open("/workspace/logs/openwebui.log", "w")
            bg_processes[app_name] = subprocess.Popen(["/workspace/venv/bin/open-webui", "serve", "--host", "0.0.0.0", "--port", "8082"], env=env, stdout=log_file, stderr=subprocess.STDOUT)
            yield log_output + "✅ Open-WebUI is running! (Port 8082)\n"

        elif app_name == "Ollama":
            if not shutil.which("ollama"):
                log_output += "📦 Installing Ollama binary...\n"; yield log_output
                subprocess.run(["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"])
            env["OLLAMA_HOST"] = "0.0.0.0:11434"
            log_output += "\n🚀 Starting Ollama Core...\n"; yield log_output
            log_file = open("/workspace/logs/ollama.log", "w")
            bg_processes[app_name] = subprocess.Popen(["ollama", "serve"], env=env, stdout=log_file, stderr=subprocess.STDOUT)
            yield log_output + "✅ Ollama is running! (Port 11434)\n"

        elif app_name == "Langflow":
            log_output += "📦 Ensuring Langflow is installed...\n"; yield log_output
            for line in run_cmd_with_logs([VENV_PIP, "pip", "install", "--python", VENV_PYTHON, "langflow"]): log_output += line; yield log_output
            log_output += "\n🚀 Starting Langflow on port 3000...\n"; yield log_output
            log_file = open("/workspace/logs/langflow.log", "w")
            bg_processes[app_name] = subprocess.Popen(["/workspace/venv/bin/langflow", "run", "--host", "0.0.0.0", "--port", "3000"], stdout=log_file, stderr=subprocess.STDOUT)
            yield log_output + "✅ Langflow is running! (Port 3000)\n"

    except Exception as e: yield log_output + f"\n❌ Error: {str(e)}"

# ==============================================================================
# TAB 5 & 6: CLOUDFLARE & HISTORY
# ==============================================================================
def toggle_cloudflare(token, action):
    global bg_processes
    app_name = "Cloudflare"
    if action == "Stop":
        if app_name in bg_processes:
            bg_processes[app_name].terminate()
            del bg_processes[app_name]
            return "🛑 Tunnel disconnected."
        return "Tunnel not running."
    
    if action == "Start":
        if not token: return "❌ Provide a Cloudflare Tunnel Token!"
        if not os.path.exists("/usr/local/bin/cloudflared"):
            yield "⬇️ Downloading Cloudflared binary..."
            subprocess.run(["curl", "-L", "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64", "-o", "/usr/local/bin/cloudflared"])
            subprocess.run(["chmod", "+x", "/usr/local/bin/cloudflared"])
        
        yield "🔄 Connecting to Cloudflare..."
        bg_processes[app_name] = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", token])
        yield "✅ Tunnel Established! App traffic is now routing to your custom domain."

def refresh_history_ui():
    hist = load_history()
    choices = [f"{'📦 NODE' if h['is_node'] else '🗂️ MODEL'} | {h['name']} ({h['size']}) -> {h['path']}" for h in hist if os.path.exists(h['path'])]
    return gr.update(choices=choices)

def delete_selected_files(selected_strings):
    if not selected_strings: return "⚠️ No files selected.", refresh_history_ui()
    log, hist =[], load_history()
    for item in selected_strings:
        target_path = item.split(" -> ")[-1].strip()
        if os.path.exists(target_path):
            try:
                if os.path.isdir(target_path): shutil.rmtree(target_path); log.append(f"🗑️ Deleted Node: {target_path}")
                else: os.remove(target_path); log.append(f"🗑️ Deleted Model: {target_path}")
            except Exception as e: log.append(f"❌ Failed to delete: {e}")
        hist = [h for h in hist if h['path'] != target_path]
    save_history(hist)
    return "\n".join(log), refresh_history_ui()

# ==============================================================================
# UI BUILDER
# ==============================================================================
with gr.Blocks(theme=gr.themes.Soft(), title="Universal Sidecar") as demo:
    gr.Markdown("# 🛰️ The Universal Sidecar OS")
    
    with gr.Tabs():
        with gr.TabItem("🌐 Gateway Dashboard"):
            gr.Markdown("### 🔗 Quick Access Links")
            gr.Markdown("Click the buttons below to open your running apps. **Note: Make sure you have installed and started them in the App Store first!**")
            gateway_html = gr.HTML(value=get_gateway_links())

        with gr.TabItem("🚀 The App Store"):
            gr.Markdown("Click **Install & Start** to hydrate services directly into the `/workspace` volume. Installations survive pod restarts.")
            apps =[
                ("ComfyUI (Port 8188)", "ComfyUI"),
                ("Kohya_ss (Port 7860)", "Kohya_ss"),
                ("Open-WebUI (Port 8082)", "Open-WebUI"),
                ("Ollama (Port 11434)", "Ollama"),
                ("Langflow (Port 3000)", "Langflow")
            ]
            for ui_label, internal_name in apps:
                with gr.Row(variant="panel"):
                    with gr.Column(scale=1):
                        gr.Markdown(f"### {ui_label}")
                        with gr.Row():
                            start_app = gr.Button("▶️ Install & Start", variant="primary", size="sm")
                            stop_app = gr.Button("🛑 Stop", variant="stop", size="sm")
                    with gr.Column(scale=2):
                        status_log = gr.Textbox(label="Installation / Launch Log", lines=4, interactive=False)
                    
                    start_app.click(fn=app_store_action, inputs=[gr.State(internal_name), gr.State("Start")], outputs=status_log)
                    stop_app.click(fn=app_store_action, inputs=[gr.State(internal_name), gr.State("Stop")], outputs=status_log)

        with gr.TabItem("📦 Downloader & Sync"):
            gr.Markdown("**Nodes** (Raw URLs or `.git`) & **Models** (Direct URL with tags) | Auto-injects tokens.txt")
            file_input = gr.File(label="Drop sync.txt or custom_nodes.txt here", file_types=[".txt"], type="filepath")
            with gr.Row():
                start_btn = gr.Button("🚀 Start Sync", variant="primary")
                cancel_btn = gr.Button("🛑 Cancel Sync", variant="stop")
            queue_out = gr.Textbox(label="Download Queue", lines=8, interactive=False)
            output_log = gr.Textbox(label="Live Execution Log", lines=12, interactive=False)
            
            sync_event = start_btn.click(fn=sync_generator, inputs=file_input, outputs=[output_log, queue_out, file_input])
            cancel_btn.click(fn=request_cancel, outputs=output_log, cancels=[sync_event])

        with gr.TabItem("🏷️ Auto-Tagger (Florence-2)"):
            gr.Markdown("Auto-captions a folder of images. **Model loads to VRAM, runs, then purges itself automatically!**")
            with gr.Row():
                tag_folder = gr.Textbox(label="Dataset Directory Path", value="/workspace/dataset", placeholder="/workspace/...")
                tag_prompt = gr.Dropdown(label="Detail Level", choices=["<CAPTION>", "<DETAILED_CAPTION>", "<MORE_DETAILED_CAPTION>"], value="<DETAILED_CAPTION>")
            tag_btn = gr.Button("🧠 Start Auto-Tagging", variant="primary")
            tag_log = gr.Textbox(label="Inference Logs", lines=10)
            tag_btn.click(fn=run_florence_tagger, inputs=[tag_folder, tag_prompt], outputs=tag_log)

        with gr.TabItem("🛡️ Cloudflare Tunnels"):
            gr.Markdown("Bypass port limits by mapping internal apps directly to your domain securely.")
            with gr.Row():
                cf_token = gr.Textbox(label="Cloudflare Tunnel Token (ey...)")
                cf_start = gr.Button("🌉 Connect Tunnel", variant="primary")
                cf_stop = gr.Button("🛑 Disconnect", variant="stop")
            cf_log = gr.Textbox(label="Tunnel Status")
            cf_start.click(fn=toggle_cloudflare, inputs=[cf_token, gr.State("Start")], outputs=cf_log)
            cf_stop.click(fn=toggle_cloudflare, inputs=[cf_token, gr.State("Stop")], outputs=cf_log)

        with gr.TabItem("🗑️ History & Cleanup"):
            gr.Markdown("Manage and delete downloaded files from the disk to free up cloud storage.")
            with gr.Row():
                refresh_history_btn = gr.Button("🔄 Refresh List", variant="primary")
                delete_btn = gr.Button("🧨 Delete Selected Files", variant="stop")
            history_cbg = gr.CheckboxGroup(label="Downloaded Items", choices=[])
            delete_log = gr.Textbox(label="Cleanup Log", lines=6, interactive=False)
            
            refresh_history_btn.click(fn=refresh_history_ui, outputs=history_cbg)
            delete_btn.click(fn=delete_selected_files, inputs=history_cbg, outputs=[delete_log, history_cbg])
            demo.load(fn=refresh_history_ui, outputs=history_cbg)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=8080)
