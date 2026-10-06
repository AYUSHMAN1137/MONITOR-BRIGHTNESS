import ctypes
from ctypes import wintypes
import math
import os
import sys
import threading
import time

try:
    import customtkinter as ctk
except ImportError:
    import tkinter as tk
    from tkinter import ttk

try:
    import screen_brightness_control as sbc
except ImportError:
    sbc = None

# Windows Win32 API definitions
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dxva2 = ctypes.windll.dxva2

class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('DeviceName', wintypes.WCHAR * 32),
        ('DeviceString', wintypes.WCHAR * 128),
        ('StateFlags', wintypes.DWORD),
        ('DeviceID', wintypes.WCHAR * 128),
        ('DeviceKey', wintypes.WCHAR * 128),
    ]

# Keep track of states per device
_display_states = {}

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

def apply_gamma(device_name, brightness=100, contrast=50, warmth=0):
    hdc = gdi32.CreateDCW(None, device_name, None, None)
    if not hdc:
        return False
    
    _display_states[device_name] = {
        "brightness": brightness,
        "contrast": contrast,
        "warmth": warmth
    }
    
    b_factor = max(0.01, brightness / 100.0)
    c_factor = max(0.1, contrast / 50.0)
    w_factor = warmth / 100.0
    
    r_mult = 1.0
    g_mult = max(0.2, 1.0 - (w_factor * 0.22))
    b_mult = max(0.1, 1.0 - (w_factor * 0.55))
    
    ramp = (wintypes.WORD * 768)()
    for i in range(256):
        val = i / 255.0
        val_c = ((val - 0.5) * c_factor) + 0.5
        val_c = max(0.0, min(1.0, val_c))
        
        r_val = int(max(0, min(65535, val_c * b_factor * r_mult * 65535)))
        g_val = int(max(0, min(65535, val_c * b_factor * g_mult * 65535)))
        b_val = int(max(0, min(65535, val_c * b_factor * b_mult * 65535)))
        
        ramp[i] = r_val
        ramp[i + 256] = g_val
        ramp[i + 512] = b_val
        
    res = gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
    gdi32.DeleteDC(hdc)
    return bool(res)

def reset_display(device_name):
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
    _display_states[device_name] = {"brightness": 100, "contrast": 50, "warmth": 0}
    if sbc:
        try:
            sbc.set_brightness(100)
        except Exception:
            pass
    return bool(res)

def set_hw_brightness(device_name, value):
    if not sbc:
        return
    try:
        devices = get_display_devices()
        target_dev = next((d for d in devices if d["device_name"] == device_name), None)
        if target_dev:
            display_idx = 0 if target_dev["is_external"] else 1
            sbc.set_brightness(int(value), display=display_idx)
        else:
            sbc.set_brightness(int(value))
    except Exception:
        pass


class MonitorBrightnessApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Appearance & Window Configuration
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Monitor Brightness Controller")
        self.geometry("480x580")
        self.resizable(False, False)

        # Set App Icon / Attributes
        self.attributes('-topmost', False)
        
        # Debounce timer for smooth slider dragging
        self._debounce_timer = None
        self._hw_timer = None

        # Fetch devices
        self.devices = get_display_devices()
        
        # Determine initial selection: prefer external monitor
        self.selected_device_name = "all"
        ext_dev = next((d for d in self.devices if d["is_external"]), None)
        if ext_dev:
            self.selected_device_name = ext_dev["device_name"]
        elif self.devices:
            self.selected_device_name = self.devices[0]["device_name"]

        self._build_ui()
        self._load_current_values()

    def _build_ui(self):
        # Main container with padding
        self.main_frame = ctk.CTkFrame(self, corner_radius=16, fg_color=("#1e293b", "#0f172a"))
        self.main_frame.pack(fill="both", expand=True, padx=16, pady=16)

        # Header Title & Subtitle
        header_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        header_frame.pack(fill="x", padx=16, pady=(12, 10))

        title_label = ctk.CTkLabel(
            header_frame,
            text="☀ Monitor Brightness",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=("#f8fafc", "#ffffff")
        )
        title_label.pack(anchor="w")

        subtitle_label = ctk.CTkLabel(
            header_frame,
            text="External Screen & Hardware DDC/CI Control",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color="#94a3b8"
        )
        subtitle_label.pack(anchor="w")

        # Monitor Selection Dropdown / Segmented
        mon_frame = ctk.CTkFrame(self.main_frame, corner_radius=10, fg_color=("#334155", "#1e293b"))
        mon_frame.pack(fill="x", padx=16, pady=(0, 12))

        mon_label = ctk.CTkLabel(
            mon_frame,
            text="Target Display:",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color="#cbd5e1"
        )
        mon_label.pack(anchor="w", padx=12, pady=(8, 4))

        options_map = {}
        display_names = []
        for d in self.devices:
            name = d["label"]
            options_map[name] = d["device_name"]
            display_names.append(name)
        
        display_names.append("All Displays")
        options_map["All Displays"] = "all"
        self.options_map = options_map

        initial_val = display_names[0]
        for name, dev_id in options_map.items():
            if dev_id == self.selected_device_name:
                initial_val = name
                break

        self.mon_menu = ctk.CTkOptionMenu(
            mon_frame,
            values=display_names,
            command=self._on_display_changed,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color="#0284c7",
            button_color="#0369a1",
            button_hover_color="#075985",
            corner_radius=8
        )
        self.mon_menu.set(initial_val)
        self.mon_menu.pack(fill="x", padx=12, pady=(0, 10))

        # Main Controls Frame
        controls_frame = ctk.CTkFrame(self.main_frame, corner_radius=12, fg_color=("#334155", "#1e293b"))
        controls_frame.pack(fill="x", padx=16, pady=6)

        # Brightness Slider Section
        b_header = ctk.CTkFrame(controls_frame, fg_color="transparent")
        b_header.pack(fill="x", padx=14, pady=(12, 4))

        ctk.CTkLabel(
            b_header,
            text="Brightness",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color="#f8fafc"
        ).pack(side="left")

        self.brightness_label = ctk.CTkLabel(
            b_header,
            text="85%",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color="#38bdf8"
        )
        self.brightness_label.pack(side="right")

        self.brightness_slider = ctk.CTkSlider(
            controls_frame,
            from_=1,
            to=100,
            number_of_steps=99,
            command=self._on_brightness_change,
            progress_color="#0284c7",
            button_color="#38bdf8",
            button_hover_color="#7dd3fc"
        )
        self.brightness_slider.set(85)
        self.brightness_slider.pack(fill="x", padx=14, pady=(0, 10))

        # Quick step buttons (-10% / +10%)
        step_frame = ctk.CTkFrame(controls_frame, fg_color="transparent")
        step_frame.pack(fill="x", padx=14, pady=(0, 12))

        btn_m10 = ctk.CTkButton(
            step_frame,
            text="-10%",
            width=70,
            height=28,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#475569",
            hover_color="#64748b",
            command=lambda: self._step_brightness(-10)
        )
        btn_m10.pack(side="left")

        btn_p10 = ctk.CTkButton(
            step_frame,
            text="+10%",
            width=70,
            height=28,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#475569",
            hover_color="#64748b",
            command=lambda: self._step_brightness(10)
        )
        btn_p10.pack(side="left", padx=8)

        # Warmth / Eye Care Slider
        w_header = ctk.CTkFrame(controls_frame, fg_color="transparent")
        w_header.pack(fill="x", padx=14, pady=(4, 4))

        ctk.CTkLabel(
            w_header,
            text="Eye-Care Warmth (Blue Light)",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color="#f8fafc"
        ).pack(side="left")

        self.warmth_label = ctk.CTkLabel(
            w_header,
            text="0%",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color="#fbbf24"
        )
        self.warmth_label.pack(side="right")

        self.warmth_slider = ctk.CTkSlider(
            controls_frame,
            from_=0,
            to=100,
            number_of_steps=100,
            command=self._on_warmth_change,
            progress_color="#d97706",
            button_color="#f59e0b",
            button_hover_color="#fbbf24"
        )
        self.warmth_slider.set(0)
        self.warmth_slider.pack(fill="x", padx=14, pady=(0, 14))

        # Presets Section
        preset_title = ctk.CTkLabel(
            self.main_frame,
            text="Quick Presets",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color="#94a3b8"
        )
        preset_title.pack(anchor="w", padx=18, pady=(4, 4))

        preset_grid = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        preset_grid.pack(fill="x", padx=16, pady=(0, 8))

        presets = [
            ("🌙 Night", 20, 60),
            ("📖 Read", 40, 35),
            ("💻 Work", 65, 10),
            ("☀️ Max", 100, 0),
            ("🍃 Eco", 30, 0)
        ]

        for text, b_val, w_val in presets:
            btn = ctk.CTkButton(
                preset_grid,
                text=text,
                height=32,
                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                fg_color="#334155",
                hover_color="#475569",
                command=lambda b=b_val, w=w_val: self._apply_preset(b, w)
            )
            btn.pack(side="left", expand=True, fill="x", padx=3)

        # Bottom Action Bar
        bottom_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        bottom_frame.pack(fill="x", padx=16, pady=(10, 0))

        btn_reset = ctk.CTkButton(
            bottom_frame,
            text="↺ Reset Defaults",
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
            command=self.destroy
        )
        btn_close.pack(side="right", expand=True, fill="x", padx=(6, 0))

    def _on_display_changed(self, choice):
        self.selected_device_name = self.options_map.get(choice, "all")
        self._load_current_values()

    def _load_current_values(self):
        dev = self.selected_device_name
        if dev == "all" and self.devices:
            dev = self.devices[0]["device_name"]
        
        state = _display_states.get(dev, {"brightness": 85, "contrast": 50, "warmth": 0})
        b = state["brightness"]
        w = state["warmth"]
        
        self.brightness_slider.set(b)
        self.brightness_label.configure(text=f"{int(b)}%")
        self.warmth_slider.set(w)
        self.warmth_label.configure(text=f"{int(w)}%")

    def _step_brightness(self, delta):
        curr = int(self.brightness_slider.get())
        new_val = max(1, min(100, curr + delta))
        self.brightness_slider.set(new_val)
        self._on_brightness_change(new_val)

    def _on_brightness_change(self, value):
        val_int = int(value)
        self.brightness_label.configure(text=f"{val_int}%")
        self._queue_apply()

    def _on_warmth_change(self, value):
        val_int = int(value)
        self.warmth_label.configure(text=f"{val_int}%")
        self._queue_apply()

    def _apply_preset(self, b_val, w_val):
        self.brightness_slider.set(b_val)
        self.brightness_label.configure(text=f"{b_val}%")
        self.warmth_slider.set(w_val)
        self.warmth_label.configure(text=f"{w_val}%")
        self._execute_apply()

    def _queue_apply(self):
        # Debounce to prevent lag during rapid slider sliding
        if self._debounce_timer:
            self.after_cancel(self._debounce_timer)
        self._debounce_timer = self.after(30, self._execute_apply)

    def _execute_apply(self):
        b = int(self.brightness_slider.get())
        w = int(self.warmth_slider.get())
        
        target = self.selected_device_name
        targets = []
        if target == "all":
            targets = [d["device_name"] for d in self.devices]
        else:
            targets = [target]

        for dev_name in targets:
            apply_gamma(dev_name, brightness=b, contrast=50, warmth=w)

        # Send hardware DDC/CI in background thread
        def hw_worker():
            for dev_name in targets:
                set_hw_brightness(dev_name, b)
        threading.Thread(target=hw_worker, daemon=True).start()

    def _reset_all(self):
        target = self.selected_device_name
        targets = [d["device_name"] for d in self.devices] if target == "all" else [target]
        for dev_name in targets:
            reset_display(dev_name)
        
        self.brightness_slider.set(100)
        self.brightness_label.configure(text="100%")
        self.warmth_slider.set(0)
        self.warmth_label.configure(text="0%")


if __name__ == "__main__":
    app = MonitorBrightnessApp()
    app.mainloop()
