# list_ports_all.py
import re
import serial.tools.list_ports

def list_all_ports():
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("⚠️ COMポートが１つも見つかりませんでした。")
        return

    print("=== Available COM ports ===")
    for port in ports:
        # ポート情報の基本表示
        print(f"Device: {port.device}")
        print(f"  Description: {port.description}")
        print(f"  HWID:        {port.hwid}")
        print(f"  VID:PID:     {hex(port.vid) if port.vid else 'N/A'}:{hex(port.pid) if port.pid else 'N/A'}")
        print(f"  serial_number: {port.serial_number}")
        # HWID中のSER=XXXX を抜き出し
        m = re.search(r"SER=([A-Z0-9]+)", port.hwid or "")
        if m:
            print(f"  Extracted SN from HWID: {m.group(1)}")
        print("----------------------------")

if __name__ == "__main__":
    list_all_ports()
