import ctypes
from ctypes import wintypes
import json
import os
import socket
import sys
import threading
import time

try:
    import customtkinter as ctk
except ImportError:
    import tkinter as ctk

# Single-instance lock to prevent multiple windows opening
LOCK_PORT = 58219
_lock_socket = None

def acquire_lock():
    global _lock_socket
    try:
        _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_socket.bind(('127.0.0.1', LOCK_PORT))
        return True
    except Exception:
        return False

# Windows GDI API
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('DeviceName', wintypes.WCHAR * 32),
        ('DeviceString', wintypes.WCHAR * 128),
        ('StateFlags', wintypes.DWORD),
        ('DeviceID', wintypes.WCHAR * 128),
        ('DeviceKey', wintypes.WCHAR * 128),
    ]

def get_display_devices():
    devices = []
    i = 0
    while True:
        d = DISPLAY_DEVICE()
        d.cb = ctypes.sizeof(DISPLAY_DEVICE)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            break
        
        is_attached = bool(d.StateFlags & 1)
        is_primary = bool(d.StateFlags & 4)
        
        if is_attached:
            gpu_name = d.DeviceString
            dev_name = d.DeviceName
            
            mon_name = "Monitor"
            mon_id = ""
            d_mon = DISPLAY_DEVICE()
            d_mon.cb = ctypes.sizeof(DISPLAY_DEVICE)
            if user32.EnumDisplayDevicesW(dev_name, 0, ctypes.byref(d_mon), 0):
                mon_name = d_mon.DeviceString or "Generic Monitor"
                mon_id = d_mon.DeviceID or ""
            
            is_internal = "BOE" in mon_id.upper() or ("INTEL" in gpu_name.upper() and "DISPLAY1" in dev_name)
            is_external = not is_internal or "NVIDIA" in gpu_name.upper() or "TXD" in mon_id.upper()
            
            if is_external:
                label = f"External Monitor ({mon_name})"
            else:
                label = f"Laptop Screen ({mon_name})"
                
            devices.append({
                "id": dev_name,
                "label": label,
                "device_name": dev_name,
                "gpu": gpu_name,
                "monitor_name": mon_name,
                "is_primary": is_primary,
                "is_external": is_external,
            })
        i += 1
    return devices

def apply_safe_gamma(device_name, slider_pct, warmth_pct=0):
    # slider_pct: 0 (minimum safe dim level) to 100 (maximum brightness)
    # Maps slider 0..100 to factor 0.52..1.0 so Windows NEVER rejects the call
    hdc = gdi32.CreateDCW(None, device_name, None, None)
    if not hdc:
        return False
    
    pct = max(0, min(100, slider_pct))
    factor = 0.52 + (0.48 * (pct / 100.0))
    
    w_factor = max(0, min(100, warmth_pct)) / 100.0
    r_mult = 1.0
    g_mult = max(0.4, 1.0 - (w_factor * 0.18))
    b_mult = max(0.2, 1.0 - (w_factor * 0.45))
    
    ramp = (wintypes.WORD * 768)()
    for i in range(256):
        val = (i / 255.0) * factor
        r_val = int(max(0, min(65535, val * r_mult * 65535)))
        g_val = int(max(0, min(65535, val * g_mult * 65535)))
        b_val = int(max(0, min(65535, val * b_mult * 65535)))
        
        ramp[i] = r_val
        ramp[i + 256] = g_val
        ramp[i + 512] = b_val
        
    res = gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
    gdi32.DeleteDC(hdc)
    return bool(res)

def reset_display_gamma(device_name):
    hdc = gdi32.CreateDCW(None, device_name, None, None)
    if not hdc:
        return False
    ramp = (wintypes.WORD * 768)()
    for i in range(256):
        val = int(i * 65535 / 255)
        ramp[i] = val
        ramp[i + 256] = val
        ramp[i + 512] = val
    res = gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
    gdi32.DeleteDC(hdc)
    return bool(res)

def load_saved_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"brightness": 75, "warmth": 0}

def save_config(data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


class MonitorBrightnessApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Appearance & Theme
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Monitor Brightness Controller")
        self.geometry("480x560")
        self.resizable(False, False)

        # Config & Devices
        self.config = load_saved_config()
        self.devices = get_display_devices()

        # Target display selection (default to External Monitor)
        self.selected_device_name = "all"
        ext_dev = next((d for d in self.devices if d["is_external"]), None)
        if ext_dev:
            self.selected_device_name = ext_dev["device_name"]
        elif self.devices:
            self.selected_device_name = self.devices[0]["device_name"]

        saved_dev = self.config.get("selected_device")
        if saved_dev and (saved_dev == "all" or any(d["device_name"] == saved_dev for d in self.devices)):
            self.selected_device_name = saved_dev

        self.current_brightness = self.config.get("brightness", 75)
        self.current_warmth = self.config.get("warmth", 0)

        self._build_ui()
        self._apply_brightness_now()

    def _build_ui(self):
        self.main_frame = ctk.CTkFrame(self, corner_radius=16, fg_color=("#1e293b", "#0f172a"))
        self.main_frame.pack(fill="both", expand=True, padx=14, pady=14)

        # Header Title
        header_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        header_frame.pack(fill="x", padx=16, pady=(10, 8))

        title_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_box.pack(side="left")

        title_label = ctk.CTkLabel(
            title_box,
            text="☀ Monitor Brightness",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color="#f8fafc"
        )
        title_label.pack(anchor="w")

        subtitle_label = ctk.CTkLabel(
            title_box,
            text="External Monitor Control",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color="#38bdf8"
        )
        subtitle_label.pack(anchor="w")

        status_badge = ctk.CTkLabel(
            header_frame,
            text="● Active",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color="#38bdf8",
            fg_color="#082f49",
            corner_radius=8,
            padx=10,
            pady=4
        )
        status_badge.pack(side="right", pady=4)

        # Target Display Selector
        mon_card = ctk.CTkFrame(self.main_frame, corner_radius=12, fg_color=("#334155", "#1e293b"))
        mon_card.pack(fill="x", padx=16, pady=(0, 10))

        mon_top = ctk.CTkFrame(mon_card, fg_color="transparent")
        mon_top.pack(fill="x", padx=14, pady=(8, 4))

        ctk.CTkLabel(
            mon_top,
            text="Target Display",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color="#94a3b8"
        ).pack(side="left")

        self.options_map = {}
        display_names = []
        for d in self.devices:
            name = d["label"]
            self.options_map[name] = d["device_name"]
            display_names.append(name)
        
        display_names.append("All Displays")
        self.options_map["All Displays"] = "all"

        initial_name = display_names[0]
        for name, dev_id in self.options_map.items():
            if dev_id == self.selected_device_name:
                initial_name = name
                break

        self.mon_menu = ctk.CTkOptionMenu(
            mon_card,
            values=display_names,
            command=self._on_display_changed,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color="#0284c7",
            button_color="#0369a1",
            button_hover_color="#075985",
            corner_radius=8,
            height=34
        )
        self.mon_menu.set(initial_name)
        self.mon_menu.pack(fill="x", padx=14, pady=(0, 10))

        # Main Controls Frame
        ctrl_card = ctk.CTkFrame(self.main_frame, corner_radius=12, fg_color=("#334155", "#1e293b"))
        ctrl_card.pack(fill="x", padx=16, pady=4)

        # Brightness Header
        b_header = ctk.CTkFrame(ctrl_card, fg_color="transparent")
        b_header.pack(fill="x", padx=14, pady=(12, 4))

        ctk.CTkLabel(
            b_header,
            text="Brightness Level",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color="#f8fafc"
        ).pack(side="left")

        self.brightness_label = ctk.CTkLabel(
            b_header,
            text=f"{int(self.current_brightness)}%",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color="#38bdf8"
        )
        self.brightness_label.pack(side="right")

        # Brightness Slider (0 to 100)
        self.brightness_slider = ctk.CTkSlider(
            ctrl_card,
            from_=0,
            to=100,
            number_of_steps=100,
            command=self._on_brightness_slider_move,
            progress_color="#0284c7",
            button_color="#38bdf8",
            button_hover_color="#7dd3fc",
            height=20
        )
        self.brightness_slider.set(self.current_brightness)
        self.brightness_slider.pack(fill="x", padx=14, pady=(2, 10))

        # Quick Step Buttons
        step_box = ctk.CTkFrame(ctrl_card, fg_color="transparent")
        step_box.pack(fill="x", padx=14, pady=(0, 12))

        for delta, text in [(-10, "-10%"), (-5, "-5%"), (+5, "+5%"), (+10, "+10%")]:
            btn = ctk.CTkButton(
                step_box,
                text=text,
                height=28,
                font=ctk.CTkFont(size=11, weight="bold"),
                fg_color="#475569",
                hover_color="#64748b",
                command=lambda d=delta: self._step_brightness(d)
            )
            btn.pack(side="left", expand=True, fill="x", padx=2)

        # Warmth / Eye Care
        w_header = ctk.CTkFrame(ctrl_card, fg_color="transparent")
        w_header.pack(fill="x", padx=14, pady=(4, 4))

        ctk.CTkLabel(
            w_header,
            text="Eye-Care Warmth (Night Light)",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color="#f8fafc"
        ).pack(side="left")

        self.warmth_label = ctk.CTkLabel(
            w_header,
            text=f"{int(self.current_warmth)}%",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color="#fbbf24"
        )
        self.warmth_label.pack(side="right")

        self.warmth_slider = ctk.CTkSlider(
            ctrl_card,
            from_=0,
            to=100,
            number_of_steps=100,
            command=self._on_warmth_slider_move,
            progress_color="#d97706",
            button_color="#f59e0b",
            button_hover_color="#fbbf24",
            height=18
        )
        self.warmth_slider.set(self.current_warmth)
        self.warmth_slider.pack(fill="x", padx=14, pady=(2, 14))

        # Presets Section
        preset_title = ctk.CTkLabel(
            self.main_frame,
            text="Quick Presets",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color="#94a3b8"
        )
        preset_title.pack(anchor="w", padx=18, pady=(4, 2))

        preset_grid = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        preset_grid.pack(fill="x", padx=16, pady=(0, 10))

        presets = [
            ("🌙 Night", 10, 60),
            ("📖 Read", 35, 30),
            ("💻 Work", 65, 10),
            ("☀️ Max", 100, 0),
            ("🍃 Eco", 25, 0)
        ]

        for text, b_val, w_val in presets:
            btn = ctk.CTkButton(
                preset_grid,
                text=text,
                height=32,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                fg_color="#334155",
                hover_color="#475569",
                command=lambda b=b_val, w=w_val: self._apply_preset(b, w)
            )
            btn.pack(side="left", expand=True, fill="x", padx=2)

        # Bottom Bar
        bottom_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        bottom_frame.pack(fill="x", padx=16, pady=(4, 6))

        btn_reset = ctk.CTkButton(
            bottom_frame,
            text="↺ Reset (100%)",
            height=34,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            fg_color="#475569",
            hover_color="#64748b",
            command=self._reset_all
        )
        btn_reset.pack(side="left", expand=True, fill="x", padx=(0, 6))

        btn_close = ctk.CTkButton(
            bottom_frame,
            text="Close",
            height=34,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            fg_color="#e11d48",
            hover_color="#be123c",
            command=self._on_close
        )
        btn_close.pack(side="right", expand=True, fill="x", padx=(6, 0))

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_display_changed(self, choice):
        self.selected_device_name = self.options_map.get(choice, "all")
        self._apply_brightness_now()

    def _step_brightness(self, delta):
        curr = int(self.brightness_slider.get())
        new_val = max(0, min(100, curr + delta))
        self.brightness_slider.set(new_val)
        self.brightness_label.configure(text=f"{new_val}%")
        self.current_brightness = new_val
        self._apply_brightness_now()

    def _on_brightness_slider_move(self, value):
        val_int = int(value)
        self.brightness_label.configure(text=f"{val_int}%")
        self.current_brightness = val_int
        self._apply_brightness_now()

    def _on_warmth_slider_move(self, value):
        val_int = int(value)
        self.warmth_label.configure(text=f"{val_int}%")
        self.current_warmth = val_int
        self._apply_brightness_now()

    def _apply_preset(self, b_val, w_val):
        self.brightness_slider.set(b_val)
        self.brightness_label.configure(text=f"{b_val}%")
        self.warmth_slider.set(w_val)
        self.warmth_label.configure(text=f"{w_val}%")
        self.current_brightness = b_val
        self.current_warmth = w_val
        self._apply_brightness_now()

    def _apply_brightness_now(self):
        b = self.current_brightness
        w = self.current_warmth
        target = self.selected_device_name

        targets = [d["device_name"] for d in self.devices] if target == "all" else [target]
        for dev_name in targets:
            apply_safe_gamma(dev_name, slider_pct=b, warmth_pct=w)

    def _reset_all(self):
        self.current_brightness = 100
        self.current_warmth = 0
        self.brightness_slider.set(100)
        self.brightness_label.configure(text="100%")
        self.warmth_slider.set(0)
        self.warmth_label.configure(text="0%")
        
        target = self.selected_device_name
        targets = [d["device_name"] for d in self.devices] if target == "all" else [target]
        for dev_name in targets:
            reset_display_gamma(dev_name)

    def _on_close(self):
        save_config({
            "selected_device": self.selected_device_name,
            "brightness": self.current_brightness,
            "warmth": self.current_warmth
        })
        self.destroy()


if __name__ == "__main__":
    if not acquire_lock():
        sys.exit(0)
    app = MonitorBrightnessApp()
    app.mainloop()
