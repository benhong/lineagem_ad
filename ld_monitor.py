import subprocess
import threading
import time
import queue
import re
import os
import locale
import tkinter as tk
from tkinter import ttk, messagebox


# ============================================================
# 基本設定
# ============================================================

DEFAULT_LDCONSOLE = r"C:\LDPlayer\LDPlayer9\ldconsole.exe"

PACKAGE_NAME = "com.gamania.lineagem"

MIN_INDEX = 1
MAX_INDEX = 30

GAME_PORT = 12000
AD_PORT = 12030

TRIAL_END_DATE = 20260919

CLOSE_RESOURCE_ID = (
    "com.gamania.lineagem:id/ncmop_campaign_close"
)


# ============================================================
# LDPlayer Monitor
# ============================================================

class LDPlayerMonitor:

    def __init__(
        self,
        ldconsole_path,
        interval,
        event_queue
    ):
        self.ldconsole_path = ldconsole_path
        self.interval = interval
        self.event_queue = event_queue

        self.running = False
        self.thread = None

        # 每台模擬器自己的狀態
        #
        # {
        #   1: {
        #       "game_pid": 12345,
        #       "ld_pid": 60084
        #   }
        # }
        self.states = {}

    # --------------------------------------------------------
    # Logging
    # --------------------------------------------------------

    def log(self, message):
        now = time.strftime("%H:%M:%S")

        self.event_queue.put({
            "type": "log",
            "message": f"[{now}] {message}"
        })

    def update_instance(
        self,
        index,
        title="",
        ld_pid="",
        app_pid="",
        status="",
        action=""
    ):
        self.event_queue.put({
            "type": "instance",
            "index": index,
            "title": title,
            "ld_pid": ld_pid,
            "app_pid": app_pid,
            "status": status,
            "action": action,
            "time": time.strftime("%H:%M:%S")
        })

    # --------------------------------------------------------
    # Command
    # --------------------------------------------------------

    def run_command(self, args, timeout=15):

        try:
            creationflags = 0

            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                creationflags=creationflags
            )

            raw = result.stdout

            # LDPlayer 有時候中文環境編碼不同
            encodings = [
                "utf-8",
                locale.getpreferredencoding(False),
                "mbcs"
            ]

            for encoding in encodings:
                try:
                    return raw.decode(encoding)
                except Exception:
                    pass

            return raw.decode(
                "utf-8",
                errors="ignore"
            )

        except subprocess.TimeoutExpired:
            return ""

        except Exception as e:
            self.log(
                f"Command Error: {e}"
            )

            return ""

    def ldconsole(self, *args):

        return self.run_command([
            self.ldconsole_path,
            *args
        ])

    def adb(self, index, command, timeout=15):

        return self.run_command(
            [
                self.ldconsole_path,
                "adb",
                "--index",
                str(index),
                "--command",
                command
            ],
            timeout=timeout
        )

    # ========================================================
    # list2
    # ========================================================

    def get_running_instances(self):

        output = self.ldconsole("list2")

        result = []

        for line in output.splitlines():

            line = line.strip()

            if not line:
                continue

            parts = line.split(",")

            if len(parts) < 10:
                continue

            try:
                instance = {
                    "index": int(parts[0]),
                    "title": parts[1],
                    "top_hwnd": int(parts[2]),
                    "bind_hwnd": int(parts[3]),
                    "running": int(parts[4]),
                    "process_pid": int(parts[5]),
                    "vbox_pid": int(parts[6]),
                    "width": int(parts[7]),
                    "height": int(parts[8]),
                    "dpi": int(parts[9])
                }

            except ValueError:
                continue

            index = instance["index"]

            # 只處理 1 ~ 30
            if not (
                MIN_INDEX <= index <= MAX_INDEX
            ):
                continue

            # running 必須 = 1
            if instance["running"] != 1:
                continue

            result.append(instance)

        return result

    # ========================================================
    # Android APP PID
    # ========================================================

    def get_app_pid(self, index):

        output = self.adb(
            index,
            f"shell pidof {PACKAGE_NAME}",
            timeout=8
        )

        output = output.strip()

        if not output:
            return None

        # 正常 pidof 可能：
        #
        # 12345
        #
        # 或：
        #
        # 12345 12346

        for line in output.splitlines():

            line = line.strip()

            if re.fullmatch(
                r"\d+(?:\s+\d+)*",
                line
            ):
                return int(
                    line.split()[0]
                )

        return None

    # ========================================================
    # TCP
    # ========================================================

    def get_connections(self, index):

        output = self.adb(
            index,
            "shell netstat -tn",
            timeout=10
        )

        return output

    @staticmethod
    def has_port(connections, port):

        pattern = rf":{port}(?:\s|$)"

        for line in connections.splitlines():

            # 只有已建立的 TCP 連線才算，忽略 LISTEN、
            # TIME_WAIT、CLOSE_WAIT 等其他狀態。
            if not re.search(
                r"\bESTABLISHED\b",
                line,
                re.IGNORECASE
            ):
                continue

            if re.search(pattern, line):
                return True

        return False

    # ========================================================
    # UI Automator
    # ========================================================

    def try_close_campaign(self, index):

        while self.running:

            # 在模擬器內產生 XML 並直接找出關閉按鈕節點。
            # 只傳回不含中文的 resource-id 與 bounds，避免完整 XML
            # 經過 ldconsole / Windows 編碼轉換後產生亂碼。
            target_data = self.adb(
                index,
                (
                    "shell uiautomator dump "
                    "/sdcard/window.xml "
                    ">/dev/null 2>&1 "
                    "&& cat /sdcard/window.xml "
                    "| tr '>' '\\n' "
                    "| grep '"
                    f"{CLOSE_RESOURCE_ID}"
                    "'"
                ),
                timeout=20
            )

            # 找不到代表目前已沒有待關閉的廣告。
            if not target_data.strip():
                return

            match = re.search(
                r'bounds="\[(\d+),(\d+)\]'
                r'\[(\d+),(\d+)\]"',
                target_data
            )

            if not match:

                self.log(
                    f"[{index:02d}] "
                    f"關閉按鈕 bounds 無法解析："
                    f"{target_data.strip()}"
                )

                return

            x1, y1, x2, y2 = map(
                int,
                match.groups()
            )

            bounds = (
                f"[{x1},{y1}]"
                f"[{x2},{y2}]"
            )

            x = (x1 + x2) // 2
            y = (y1 + y2) // 2

            self.log(
                f"[{index:02d}] "
                f"找到廣告關閉按鈕 "
                f"{bounds} -> 點擊 ({x},{y})"
            )

            self.adb(
                index,
                f"shell input tap {x} {y}",
                timeout=5
            )

            # 等待下一個廣告視窗出現，再重新取得 XML 檢查。
            wait_until = time.time() + 1

            while (
                self.running
                and time.time() < wait_until
            ):
                time.sleep(0.1)

    # ========================================================
    # 單台模擬器監控
    # ========================================================

    def monitor_instance(self, instance):

        index = instance["index"]
        title = instance["title"]

        ld_pid = instance[
            "process_pid"
        ]

        state = self.states.setdefault(
            index,
            {
                "game_pid": None,
                "ld_pid": ld_pid
            }
        )

        # ----------------------------------------------------
        # LDPlayer 本身 PID 改變
        # 代表模擬器可能重新啟動
        # ----------------------------------------------------

        if (
            state["ld_pid"]
            and state["ld_pid"] != ld_pid
        ):

            self.log(
                f"[{index:02d}] "
                f"模擬器 PID 改變 "
                f"{state['ld_pid']} -> {ld_pid}"
            )

            state["game_pid"] = None

        state["ld_pid"] = ld_pid

        # ----------------------------------------------------
        # APP PID
        # ----------------------------------------------------

        app_pid = self.get_app_pid(
            index
        )

        if app_pid is None:

            if state["game_pid"] is not None:

                self.log(
                    f"[{index:02d}] "
                    f"遊戲已停止"
                )

            state["game_pid"] = None

            self.update_instance(
                index,
                title,
                ld_pid,
                "",
                "APP 未開啟",
                "等待"
            )

            return

        # ====================================================
        # 已經確認進入遊戲
        # ====================================================

        if state["game_pid"] is not None:

            # PID 沒變
            #
            # 不查 PORT
            # 不跑 uiautomator

            if (
                app_pid
                == state["game_pid"]
            ):

                self.update_instance(
                    index,
                    title,
                    ld_pid,
                    app_pid,
                    "遊戲中",
                    "PID 正常"
                )

                return

            # PID 改變
            self.log(
                f"[{index:02d}] "
                f"遊戲 PID 改變 "
                f"{state['game_pid']} "
                f"-> {app_pid}"
            )

            state["game_pid"] = None

            # 不 return
            # 直接重新做完整檢查

        # ====================================================
        # 尚未確認是否進入遊戲
        # ====================================================

        connections = self.get_connections(
            index
        )

        has_12000 = self.has_port(
            connections,
            GAME_PORT
        )

        has_12030 = self.has_port(
            connections,
            AD_PORT
        )

        # ----------------------------------------------------
        # 12000
        # ----------------------------------------------------

        if has_12000:

            state["game_pid"] = app_pid

            self.log(
                f"[{index:02d}] "
                f"偵測到 Port {GAME_PORT}，"
                f"進入遊戲，記錄 PID={app_pid}"
            )

            self.update_instance(
                index,
                title,
                ld_pid,
                app_pid,
                "遊戲中",
                f"Port {GAME_PORT}"
            )

            return

        # ----------------------------------------------------
        # 12030
        # ----------------------------------------------------

        if has_12030:

            self.update_instance(
                index,
                title,
                ld_pid,
                app_pid,
                "登入 / 廣告階段",
                f"Port {AD_PORT} → UI檢查"
            )

            #
            # 不管有沒有廣告
            # 都只跑一次 UI 檢查
            #
            self.try_close_campaign(
                index
            )

            #
            # 找得到就關
            # 找不到也不管
            #
            # 下一輪重新檢查 Port
            #

            return

        # ----------------------------------------------------
        # 12000、12030 都未偵測到
        # 採用 12030 的動作進行 UI 檢查
        # ----------------------------------------------------

        self.update_instance(
            index,
            title,
            ld_pid,
            app_pid,
            "登入 / 廣告階段",
            f"未偵測到 Port → 依 {AD_PORT} 處理"
        )

        self.try_close_campaign(
            index
        )

    # ========================================================
    # Main loop
    # ========================================================

    def scan_once(self):

        instances = (
            self.get_running_instances()
        )

        active_indexes = set()

        for instance in instances:

            if not self.running:
                break

            index = instance["index"]

            active_indexes.add(index)

            try:
                self.monitor_instance(
                    instance
                )

            except Exception as e:

                self.log(
                    f"[{index:02d}] "
                    f"監控錯誤: {e}"
                )

        # ----------------------------------------------
        # 之前有狀態，但現在模擬器已關閉
        # ----------------------------------------------

        for index in list(
            self.states.keys()
        ):

            if index not in active_indexes:

                self.states.pop(
                    index,
                    None
                )

                self.update_instance(
                    index,
                    "",
                    "",
                    "",
                    "模擬器未開啟",
                    ""
                )

    def loop(self):

        self.log(
            "監控已啟動"
        )

        while self.running:

            start_time = time.time()

            self.scan_once()

            elapsed = (
                time.time()
                - start_time
            )

            wait_time = max(
                0.1,
                self.interval - elapsed
            )

            # 分段 sleep
            # 讓停止按鈕反應快
            end_time = (
                time.time()
                + wait_time
            )

            while (
                self.running
                and time.time() < end_time
            ):
                time.sleep(0.2)

        self.log(
            "監控已停止"
        )

    def start(self):

        if self.running:
            return

        self.running = True

        self.thread = threading.Thread(
            target=self.loop,
            daemon=True
        )

        self.thread.start()

    def stop(self):

        self.running = False


# ============================================================
# GUI
# ============================================================

class MonitorGUI:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "LDPlayer 天堂M監控"
        )

        self.root.geometry(
            "1100x720"
        )

        self.event_queue = queue.Queue()

        self.monitor = None

        self.build_ui()

        self.root.after(
            100,
            self.process_events
        )

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.on_close
        )

    # --------------------------------------------------------

    def build_ui(self):

        # ====================================================
        # Settings
        # ====================================================

        settings = ttk.LabelFrame(
            self.root,
            text="監控設定"
        )

        settings.pack(
            fill="x",
            padx=10,
            pady=10
        )

        ttk.Label(
            settings,
            text="ldconsole.exe："
        ).grid(
            row=0,
            column=0,
            padx=5,
            pady=8,
            sticky="w"
        )

        self.path_var = tk.StringVar(
            value=DEFAULT_LDCONSOLE
        )

        ttk.Entry(
            settings,
            textvariable=self.path_var,
            width=65
        ).grid(
            row=0,
            column=1,
            padx=5,
            pady=8
        )

        ttk.Label(
            settings,
            text="檢查間隔："
        ).grid(
            row=0,
            column=2,
            padx=5
        )

        self.interval_var = tk.StringVar(
            value="5"
        )

        ttk.Entry(
            settings,
            textvariable=self.interval_var,
            width=6
        ).grid(
            row=0,
            column=3
        )

        ttk.Label(
            settings,
            text="秒"
        ).grid(
            row=0,
            column=4,
            padx=(2, 10)
        )

        self.start_button = ttk.Button(
            settings,
            text="開始監控",
            command=self.start_monitor
        )

        self.start_button.grid(
            row=0,
            column=5,
            padx=5
        )

        self.stop_button = ttk.Button(
            settings,
            text="停止",
            command=self.stop_monitor,
            state="disabled"
        )

        self.stop_button.grid(
            row=0,
            column=6,
            padx=5
        )

        # ====================================================
        # Status
        # ====================================================

        status_frame = ttk.LabelFrame(
            self.root,
            text="模擬器狀態"
        )

        status_frame.pack(
            fill="both",
            expand=True,
            padx=10,
            pady=(0, 5)
        )

        columns = (
            "index",
            "title",
            "ld_pid",
            "app_pid",
            "status",
            "action",
            "time"
        )

        self.tree = ttk.Treeview(
            status_frame,
            columns=columns,
            show="headings",
            height=18
        )

        headings = {
            "index": "編號",
            "title": "模擬器",
            "ld_pid": "LD PID",
            "app_pid": "遊戲 PID",
            "status": "狀態",
            "action": "動作",
            "time": "更新"
        }

        widths = {
            "index": 60,
            "title": 120,
            "ld_pid": 90,
            "app_pid": 90,
            "status": 150,
            "action": 250,
            "time": 80
        }

        for col in columns:

            self.tree.heading(
                col,
                text=headings[col]
            )

            self.tree.column(
                col,
                width=widths[col],
                anchor="center"
            )

        scrollbar = ttk.Scrollbar(
            status_frame,
            orient="vertical",
            command=self.tree.yview
        )

        self.tree.configure(
            yscrollcommand=scrollbar.set
        )

        self.tree.pack(
            side="left",
            fill="both",
            expand=True
        )

        scrollbar.pack(
            side="right",
            fill="y"
        )

        # ====================================================
        # Log
        # ====================================================

        log_frame = ttk.LabelFrame(
            self.root,
            text="Log"
        )

        log_frame.pack(
            fill="both",
            padx=10,
            pady=(5, 10)
        )

        self.log_text = tk.Text(
            log_frame,
            height=10,
            state="disabled"
        )

        self.log_text.pack(
            fill="both",
            expand=True
        )

    # ========================================================
    # Start
    # ========================================================

    def start_monitor(self):

        today = int(
            time.strftime("%Y%m%d")
        )

        if today > TRIAL_END_DATE:

            messagebox.showwarning(
                "免費體驗時間已到",
                (
                    "⏰ 免費體驗時間已到\n"
                    "這裡的功能暫時被鎖住了 🔒\n"
                    "不知道怎麼辦？\n"
                    "或許你可以問問 AI：\n"
                    "「這個限制要怎麼解開？」"
                )
            )

            return

        path = self.path_var.get().strip()

        if not os.path.isfile(path):

            messagebox.showerror(
                "錯誤",
                f"找不到：\n{path}"
            )

            return

        try:
            interval = float(
                self.interval_var.get()
            )

            if interval < 1:
                raise ValueError

        except ValueError:

            messagebox.showerror(
                "錯誤",
                "檢查間隔至少 1 秒"
            )

            return

        self.monitor = LDPlayerMonitor(
            path,
            interval,
            self.event_queue
        )

        self.monitor.start()

        self.start_button.configure(
            state="disabled"
        )

        self.stop_button.configure(
            state="normal"
        )

    # ========================================================
    # Stop
    # ========================================================

    def stop_monitor(self):

        if self.monitor:
            self.monitor.stop()

        self.start_button.configure(
            state="normal"
        )

        self.stop_button.configure(
            state="disabled"
        )

    # ========================================================
    # Event
    # ========================================================

    def process_events(self):

        try:

            while True:

                event = (
                    self.event_queue.get_nowait()
                )

                if event["type"] == "log":

                    self.append_log(
                        event["message"]
                    )

                elif event["type"] == "instance":

                    self.update_tree(
                        event
                    )

        except queue.Empty:
            pass

        self.root.after(
            100,
            self.process_events
        )

    def append_log(self, message):

        self.log_text.configure(
            state="normal"
        )

        self.log_text.insert(
            "end",
            message + "\n"
        )

        self.log_text.see(
            "end"
        )

        self.log_text.configure(
            state="disabled"
        )

    def update_tree(self, data):

        index = data["index"]

        iid = f"vm_{index}"

        values = (
            f"{index:02d}",
            data["title"],
            data["ld_pid"],
            data["app_pid"],
            data["status"],
            data["action"],
            data["time"]
        )

        if self.tree.exists(iid):

            self.tree.item(
                iid,
                values=values
            )

        else:

            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=values
            )

    # ========================================================

    def on_close(self):

        if self.monitor:
            self.monitor.stop()

        self.root.destroy()


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    root = tk.Tk()

    app = MonitorGUI(
        root
    )

    root.mainloop()
