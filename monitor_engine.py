import ctypes
from ctypes import wintypes
import json
import logging
import math
import sys
import threading
import time

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dxva2 = ctypes.windll.dxva2

try:
    import screen_brightness_control as sbc
except ImportError:
    sbc = None

class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('DeviceName', wintypes.WCHAR * 32),
        ('DeviceString', wintypes.WCHAR * 128),
        ('StateFlags', wintypes.DWORD),
        ('DeviceID', wintypes.WCHAR * 128),
        ('DeviceKey', wintypes.WCHAR * 128),
    ]

# Global state to keep track of software adjustments per device
_display_states = {}

def get_display_devices():
    devices = []
    i = 0
    while True:
        d = DISPLAY_DEVICE()
        d.cb = ctypes.sizeof(DISPLAY_DEVICE)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            break
        
        # StateFlags: 1 = AttachedToDesktop, 4 = PrimaryDevice
        is_attached = bool(d.StateFlags & 1)
        is_primary = bool(d.StateFlags & 4)
        
        if is_attached:
            gpu_name = d.DeviceString
            dev_name = d.DeviceName # e.g. \\.\DISPLAY5
            
            # Query attached monitor info
            mon_name = "Monitor"
            mon_id = ""
            d_mon = DISPLAY_DEVICE()
            d_mon.cb = ctypes.sizeof(DISPLAY_DEVICE)
            if user32.EnumDisplayDevicesW(dev_name, 0, ctypes.byref(d_mon), 0):
                mon_name = d_mon.DeviceString or "Generic Monitor"
                mon_id = d_mon.DeviceID or ""
            
            # Detect if external or laptop built-in
            is_internal = "BOE" in mon_id.upper() or ("INTEL" in gpu_name.upper() and "DISPLAY1" in dev_name)
            is_external = not is_internal or "NVIDIA" in gpu_name.upper() or "TXD" in mon_id.upper()
            
            # Clean friendly name
            if is_external:
                friendly = f"External Monitor ({mon_name})"
                if "NVIDIA" in gpu_name:
                    friendly = f"External Monitor (HDMI/DP - {mon_name})"
            else:
                friendly = f"Laptop Built-in Display ({mon_name})"
                
            devices.append({
                "id": dev_name,
                "device_name": dev_name,
                "friendly_name": friendly,
                "gpu": gpu_name,
                "monitor_name": mon_name,
                "monitor_id": mon_id,
                "is_primary": is_primary,
                "is_external": is_external,
            })
        i += 1
    return devices

def apply_gamma_adjustments(device_name, brightness=100, contrast=50, warmth=0):
    # brightness: 0 to 100
    # contrast: 0 to 100 (50 is normal)
    # warmth: 0 to 100 (0 normal 6500K, 100 warm 3200K night light)
    hdc = gdi32.CreateDCW(None, device_name, None, None)
    if not hdc:
        return False
    
    # Store state
    _display_states[device_name] = {
        "brightness": brightness,
        "contrast": contrast,
        "warmth": warmth
    }
    
    b_factor = max(0.01, brightness / 100.0)
    # contrast curve factor
    c_factor = max(0.1, contrast / 50.0)
    
    # Warmth factor: reduce blue and slightly green
    w_factor = warmth / 100.0
    r_mult = 1.0
    g_mult = max(0.2, 1.0 - (w_factor * 0.22))
    b_mult = max(0.1, 1.0 - (w_factor * 0.55))
    
    ramp = (wintypes.WORD * 768)()
    for i in range(256):
        val = i / 255.0
        # Contrast adjustment centered at 0.5
        val_c = ((val - 0.5) * c_factor) + 0.5
        val_c = max(0.0, min(1.0, val_c))
        
        # Linear/perceptual mapping
        val_scaled = val_c
        
        r_val = int(max(0, min(65535, val_scaled * b_factor * r_mult * 65535)))
        g_val = int(max(0, min(65535, val_scaled * b_factor * g_mult * 65535)))
        b_val = int(max(0, min(65535, val_scaled * b_factor * b_mult * 65535)))
        
        ramp[i] = r_val
        ramp[i + 256] = g_val
        ramp[i + 512] = b_val
        
    res = gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
    gdi32.DeleteDC(hdc)
    return bool(res)

def reset_gamma_ramp(device_name):
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
    return bool(res)

def set_hardware_brightness_sbc(device_name, value):
    value = max(0, min(100, int(value)))
    if not sbc:
        return False
    try:
        devices = get_display_devices()
        target_dev = next((d for d in devices if d["device_name"] == device_name), None)
        if target_dev:
            if target_dev["is_external"]:
                sbc.set_brightness(value, display=0)
            else:
                sbc.set_brightness(value, display=1)
            return True
        else:
            sbc.set_brightness(value)
            return True
    except Exception as e:
        logging.warning(f"SBC hardware brightness failed: {e}")
        return False

def get_all_status():
    devices = get_display_devices()
    sbc_info = []
    if sbc:
        try:
            sbc_info = sbc.list_monitors_info()
        except Exception:
            pass
            
    monitors_data = []
    for idx, dev in enumerate(devices):
        dev_id = dev["id"]
        # Default state
        state = _display_states.get(dev_id, {"brightness": 100, "contrast": 50, "warmth": 0})
        
        monitors_data.append({
            "id": dev_id,
            "device_name": dev["device_name"],
            "friendly_name": dev["friendly_name"],
            "gpu": dev["gpu"],
            "monitor_name": dev["monitor_name"],
            "is_primary": dev["is_primary"],
            "is_external": dev["is_external"],
            "brightness": state["brightness"],
            "contrast": state["contrast"],
            "warmth": state["warmth"],
            "sbc_index": idx
        })
    return monitors_data

if __name__ == "__main__":
    print("Detected Monitors:")
    for m in get_all_status():
        print(f" -> {m['friendly_name']} ({m['device_name']}) [External: {m['is_external']}]")
