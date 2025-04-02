from PyQt5 import QtWidgets, QtGui, QtCore
import can
import sys
import time
from PyQt5.QtWidgets import QTableWidgetItem
import threading
import struct
active_nodes = []   
import time
import can
from collections import defaultdict
import os
import traceback

def log_errors():
    with open("error_log.txt", "w") as f:
        f.write(traceback.format_exc())

class HeartbeatThread(QtCore.QThread):
    heartbeat_signal = QtCore.pyqtSignal(list)  
    error_signal = QtCore.pyqtSignal(str)
    success_signal = QtCore.pyqtSignal(str)

    def __init__(self, bus, timeout=5):
        super().__init__()
        self.bus = bus
        self.running = True
        self.timeout = timeout 
        self.received_messages = {} 
        global active_nodes
        active_nodes = []

    def run(self):
        print("🔍 Scanning for active nodes...")
        global active_nodes
        active_nodes = []  
        node_count = defaultdict(int) 

        for node_id in range(1, 128):  
            request_id = 0x600 + node_id  
            response_id = 0x580 + node_id  
            message = can.Message(
                arbitration_id=request_id,
                data=[0x40, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00],
                is_extended_id=False
            )
            try:
                self.bus.send(message)
                time.sleep(0.01)  

                received = self.bus.recv(timeout=0.1)  
                if received and received.arbitration_id == response_id:
                    node_count[node_id] += 1  

                    if node_count[node_id] == 1:
                        active_nodes.append(node_id) 
                        print(f"✅ Node {node_id} responded!")

            except can.CanError as e:
                print(f"❌ Error sending to Node {node_id}: {e}")
                self.error_signal.emit(f"Error communicating with Node {node_id}: {e}")

        print("🏁 Active nodes detected:", active_nodes)


        duplicate_nodes = [node for node, count in node_count.items() if count > 1]
        if duplicate_nodes:
            error_message = f"⚠️ Duplicate Node IDs detected: {duplicate_nodes}. Please change one!"
            print(error_message)
            self.error_signal.emit("Conflict: Two sensors are using the same node ID. Please change one.")

        if active_nodes:
            self.heartbeat_signal.emit(list(active_nodes))
        else:
            #self.error_signal.emit("Cut Off the Power of Sensor and then select proper 'Baudrate..'")
            self.error_signal.emit("Disconnect the Channel and Connect with  proper 'Baudrate..'")

        if self.bus:
            self.bus.shutdown()


    def stop(self):
        self.running = False
        self.quit()
        self.wait()  


class CANReaderThread(QtCore.QThread):
    data_received = QtCore.pyqtSignal(str, str, float)
    error_detected = QtCore.pyqtSignal(int)  
    node_id_signal = QtCore.pyqtSignal(int) 

    def __init__(self, shared_bus, node_list, parent=None):
        super().__init__(parent)
        self.running = False
        self.shared_bus = shared_bus
        self.node_status = {}  
        self.node_list = node_list  
        self.current_node_ids = {}  
        self.error_node_id = None  

    def run(self):
        if not self.shared_bus:
         print("❌ Error: shared_bus is not initialized!")
         return  # Stop execution if bus is not initialized

        self.bus = self.shared_bus
        self.running = True
        print("🔗 Connecting to CAN bus...", self.bus)

        while self.running:
         try:
                message = self.bus.recv(timeout=1)
                if not message:
                  continue
                canid = hex(message.arbitration_id)
                self.node_status = {node_id: "active" for node_id in self.node_list}
                # print("Initial Node Status:", self.node_status)

                for node_id in list(self.node_status.keys()):
                    expected_canid = 0x580 + node_id
                    expected_canid_hex = hex(expected_canid)

                    if canid == expected_canid_hex:
                        raw_data = message.data.hex()
                        first_byte = message.data[0]

                        if first_byte == 0x80:  
                            print(f"🚨 Error detected for Node {node_id}. Removing from active list.")
                            self.error_node_id = node_id  
                            self.error_detected.emit(node_id) 
                            self.node_status.pop(node_id) 
                            self.current_node_ids.pop(node_id, None)  # Remove node from tracking
                            break

                        # Store multiple sensor node IDs
                        self.current_node_ids[node_id] = True
                        # print("Active Nodes:", list(self.current_node_ids.keys()))
                        self.node_id_signal.emit(node_id)

                        hex_data = ''.join(f"{byte:02X}" for byte in message.data)
                        hex_value = hex_data[8:16]  # Extract pressure data
                        binary_data = bytes.fromhex(hex_value)

                        if len(binary_data) == 4:
                            pressure_in_bar = struct.unpack("<f", binary_data)[0]
                            pressure_value = pressure_in_bar / 100
                        else:
                            print(f"⚠️ Invalid data length for Node {node_id}: {len(binary_data)} bytes")
                            continue

                        canid_str = str(message.arbitration_id - 0x580)
                        print(f"Thread Received: CAN ID: {canid_str}, Data: {raw_data}, Pressure: {pressure_value:.2f} bar")
                        self.data_received.emit(canid_str, raw_data, pressure_value)

         except can.CanError as e:
            print(f"⚠️ CAN Bus Error: {e}")
            self.running = False
            return

    def stop(self):
        self.running = False
        if self.bus:
            try:
                self.bus.shutdown()
                self.bus = None  
            except Exception as e:
                print(f"Error shutting down CAN bus: {e}")
        self.quit()
        self.wait()

class CANRequestSenderpressure(QtCore.QThread):
    def __init__(self, bus, node_ids):
        super().__init__()
        self.bus = bus
        self.node_ids = node_ids 
        self.current_index = 0  
        self.running = True 

    def switch_to_next_node(self):
        """Switch to the next available node if current node has an error."""
        if self.node_ids:
            self.current_index = (self.current_index + 1) % len(self.node_ids)
            print(f"🔄 Switching to Node {self.node_ids[self.current_index]}")

    def handle_error(self, node_id):
        """Remove failed node and switch to next valid one."""
        if node_id in self.node_ids:
            print(f"🚨 Removing Node {node_id} due to error.")
            self.node_ids.remove(node_id)
            self.switch_to_next_node()

    def run(self):
        while self.running and self.node_ids:
            node_id = self.node_ids[self.current_index]
            arbitration_id = 0x600 + node_id
            message = can.Message(arbitration_id=arbitration_id,
                                  data=[0x40, 0x30, 0x61, 0x01, 0x00, 0x00, 0x00, 0x00],
                                  is_extended_id=False)
            self.bus.send(message)
            print(f"📤 Sent request to Node {node_id}: {message.data.hex()}")
            time.sleep(0.1)

    def stop(self):
        self.running = False
        self.quit()
        self.wait()

class CANListenerRotary(QtCore.QThread):
    angle_updated = QtCore.pyqtSignal(str, float)
    error_detected_rotarry = QtCore.pyqtSignal(int)  
    node_id_signal_rotarry = QtCore.pyqtSignal(int) 

    def __init__(self, shared_bus,node_list, parent=None):
        super().__init__(parent)
        self.running = False
        self.shared_bus = shared_bus
        self.node_status_rottary = {}  
        self.node_list = node_list 
        self.current_node_id_rotarry = {}  
        self.error_node_id_rotarry = None 
        self.last_angles = {}
    def extract_position(self, data):
        if len(data) < 6:
            return 0
        byte_4 = data[4]
        byte_5 = data[5]
        position = (byte_5 << 8) + byte_4
        return position

    def calculate_angle(self, position, total_steps=65536):
        return (position / total_steps) * 360.0

    def run(self):
        self.bus_rotary = self.shared_bus
        self.running = True
        print("🔗 Connecting to CAN bus...",self.bus_rotary)

        while self.running:
          try:
            message = self.bus_rotary.recv(timeout=1)
            if not message:
                 continue
            if message:
                canid = hex(message.arbitration_id)
                self.node_status_rottary = {node_id: "active" for node_id in self.node_list}
                # print("Initial Node Status:", self.node_status_rottary)
                for node_id in list(self.node_status_rottary.keys()):
                    expected_canid = 0x580 + node_id
                    expected_canid_hex = hex(expected_canid)

                    if canid == expected_canid_hex:
                        raw_data = message.data.hex()
                        first_byte = message.data[0]

                        if first_byte == 0x80:  
                            print(f"🚨 Error detected for Node {node_id}. Removing from active list.")
                            self.error_node_id_rotarry = node_id 
                            self.error_detected_rotarry.emit(node_id)  
                            self.node_status_rottary.pop(node_id)  
                            break
                        self.current_node_id_rotarry[node_id] = True
                        # print("Active Nodes:", list(self.current_node_id_rotarry.keys()))
                        self.node_id_signal_rotarry.emit(node_id)
                        position = self.extract_position(message.data)
                        angle = self.calculate_angle(position)
                        if canid not in self.last_angles or self.last_angles[canid] != angle:
                            canid = str(message.arbitration_id - 0x580)
                            self.angle_updated.emit(canid, angle)
                            self.last_angles[canid] = angle
          except can.CanError as e:
            print(f"⚠️ CAN Bus Error: {e}")
            self.running = False
            return
 
    def stop(self):
        self.running = False
        if self.bus_rotary:
            try:
                self.bus_rotary.shutdown()
                self.bus_rotary = None  
            except Exception as e:
                print(f"Error shutting down CAN bus: {e}")
        self.quit()
        self.wait()

class CANRequestSenderRotary(QtCore.QThread):
    def __init__(self, bus, node_ids):
        super().__init__()
        self.bus = bus
        self.node_ids = node_ids  
        self.current_index = 0  
        self.running = True  
        self.initial_message_sent = {node_id: False for node_id in node_ids}  
        self.set_zero_triggered = False  # Flag for set zero command

    def switch_to_next_node(self):
        """Switch to the next available node."""
        if self.node_ids:
            self.current_index = (self.current_index + 1) % len(self.node_ids)
            print(f"🔄 Switching to Node {self.node_ids[self.current_index]}")

    def handle_error(self, node_id):
        """Remove failed node and switch to next valid one."""
        if node_id in self.node_ids:
            print(f"🚨 Removing Node {node_id} due to error.")
            self.node_ids.remove(node_id)
            self.switch_to_next_node()

    def on_setzero(self):
        """Enable flag to send Set Zero command once."""
        self.set_zero_triggered = True  

    def run(self):
        while self.running and self.node_ids:
            node_id = self.node_ids[self.current_index]
            arbitration_id = 0x600 + node_id

            if self.set_zero_triggered:
                message = can.Message(arbitration_id=arbitration_id,
                                      data=[0x23, 0x03, 0x21, 0x00, 0x00, 0x00, 0x00, 0x00],
                                      is_extended_id=False)
                self.bus.send(message)
                print(f"📤 Sent Set Zero command to Node {node_id}: {message.data.hex()}")
                self.set_zero_triggered = False  # Reset after sending

            else:
                message = can.Message(arbitration_id=arbitration_id,
                                      data=[0x40, 0x00, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00],
                                      is_extended_id=False)
                self.bus.send(message)
                print(f"📤 Sent normal request to Node {node_id}: {message.data.hex()}")

            time.sleep(0.1)
            self.switch_to_next_node()

    def stop(self):
        self.running = False
        self.quit()
        self.wait()

class CANReaderThreadDIO(QtCore.QThread):
    data_received_dio = QtCore.pyqtSignal(str, str)
    error_detected_dio = QtCore.pyqtSignal(int) 

    def __init__(self, shared_bus,node_list, parent=None):
        super().__init__(parent)
        self.runningDIO = False
        self.shared_bus = shared_bus
        self.node_status_dio = {}
        self.node_list = node_list

    def run(self):
        try:
            print("🔗 Connecting to CAN bus for DIO...",self.shared_bus)
            self.busdio = self.shared_bus
            self.runningDIO = True
        except Exception as e:
            print(f"❌ CAN Init Error: {e}")
            return

        while self.runningDIO:
            try:
                message = self.busdio.recv(timeout=1)
                if not message:
                  continue
                if message:
                    canid = hex(message.arbitration_id)
                    self.node_status_dio = {node_id: "active" for node_id in self.node_list}
                    # print("Initial Node Status:", self.node_status_dio)

                    for node_id in list(self.node_status_dio.keys()): 
                        expected_canid = 0x580 + node_id

                        if message.arbitration_id == expected_canid:
                            raw_data = message.data.hex()
                            first_byte = message.data[0] 

                            if first_byte == 0x80:  
                                print(f"🚨 Error detected for Node {node_id}. Removing...")
                                self.node_status_dio.pop(node_id)
                                self.error_detected_dio.emit(node_id)  
                                break

                            byte_4 = message.data[4]
                            binary_representation = format(byte_4, '08b')[::-1]  
                            print(f"📥 Received: {raw_data}, DIO Status: {binary_representation}")
                            canid = str(message.arbitration_id - 0x580)
                            self.data_received_dio.emit(canid, binary_representation)
            except can.CanError as e:
             print(f"⚠️ CAN Bus Error: {e}")
             self.running = False
             return

    def stop(self):
        self.runningDIO = False
        if self.busdio:
            try:
                self.busdio.shutdown()
                self.busdio = None  
                print("🛑 CAN bus stopped.")
            except Exception as e:
                print(f"Error shutting down CAN bus: {e}")
        self.quit()
        self.wait()

class CANRequestSender(QtCore.QThread):
    def __init__(self, bus, node_ids):
        super().__init__()
        self.bus = bus
        self.node_ids = node_ids  
        self.current_index = 0  
        self.running = True 

    def switch_to_next_node(self):
        """Switch to the next available node."""
        if self.node_ids:
            self.current_index = (self.current_index + 1) % len(self.node_ids)
            print(f"🔄 Switching to Node {self.node_ids[self.current_index]}")

    def handle_error(self, node_id):
        """Remove failed node and switch to next valid one."""
        if node_id in self.node_ids:
            print(f"🚨 Removing Node {node_id} due to error.")
            self.node_ids.remove(node_id)
            self.switch_to_next_node()

    def run(self):
        while self.running and self.node_ids:
            node_id = self.node_ids[self.current_index]
            arbitration_id = 0x600 + node_id
            message = can.Message(arbitration_id=arbitration_id,
                                  data=[0x40, 0x00, 0x60, 0x01, 0x00, 0x00, 0x00, 0x00],
                                  is_extended_id=False)
            self.bus.send(message)
            print(f"📤 Sent request to Node {node_id}: {message.data.hex()}")
            time.sleep(0.1)

    def stop(self):
        self.running = False
        self.quit()
        self.wait()

class CANConfigApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.eds_data = {}  
        self.setWindowTitle("CAN CONFIGURATION")
        self.setGeometry(100, 100, 1000, 600)  
        self.setStyleSheet("background-color: #f5f5f5;")  
        self.selected_channel = None
        self.selected_bitrate = None  
        self.current_node_id_dio = None
        self.busdio = None      
        if getattr(sys, 'frozen', False):
            # If running as a bundled executable
            base_path = sys._MEIPASS
        else:
            # If running as a regular script
            base_path = os.path.dirname(os.path.abspath(__file__))

        pcan_dll_path = os.path.join(base_path, 'PCANBasic.dll')   
        main_layout = QtWidgets.QVBoxLayout(self)
        header_layout = QtWidgets.QHBoxLayout()
        self.logo_label = QtWidgets.QLabel(self)  
        if getattr(sys, 'frozen', False):
            script_dir = sys._MEIPASS
        else:
            script_dir = os.path.dirname(os.path.abspath(__file__))

        image_path = os.path.join(script_dir, "logo.jpg" )
        pixmap = QtGui.QPixmap(image_path)
        self.logo_label.setPixmap(pixmap)
        self.logo_label.setScaledContents(True)
        self.logo_label.setFixedSize(100, 100)
        self.header = QtWidgets.QLabel("CAN CONFIGURATION")
        self.header.setAlignment(QtCore.Qt.AlignCenter)
        self.header.setStyleSheet("font-size: 30px; font-weight: bold; color: #2c3e50;")
        self.datetime_label = QtWidgets.QLabel("")
        self.datetime_label.setAlignment(QtCore.Qt.AlignCenter)
        self.datetime_label.setStyleSheet("font-size: 14px; color: #7f8c8d;")
        header_layout.addWidget(self.logo_label)
        header_vlayout = QtWidgets.QVBoxLayout()
        header_vlayout.setSpacing(0)
        header_vlayout.addWidget(self.header)
        header_vlayout.addWidget(self.datetime_label)
        header_layout.addLayout(header_vlayout)
        main_layout.addLayout(header_layout)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_datetime)
        self.timer.start(1000)  
        input_layout = QtWidgets.QHBoxLayout()
        self.dropdown1 = QtWidgets.QComboBox(self)
        self.populate_can_channels()  
        self.dropdown1.setFixedHeight(30)
        self.dropdown1.currentIndexChanged.connect(self.on_channel_selected)
        if self.dropdown1.count() > 0:
            self.dropdown1.setCurrentIndex(0) 
            self.selected_channel = self.dropdown1.currentText()
        self.dropdown2 = QtWidgets.QComboBox(self)
        self.dropdown2.addItems(["Select Baudrate","90000","20000", "50000", "100000", "125000", "250000", "500000", "800000", 
                                 "1000000"])
        self.dropdown2.setFixedHeight(30)
        self.dropdown2.currentIndexChanged.connect(self.on_bitrate_selected)
        # self.dropdown1.addItem("Select Channel")
        self.manual_bitrate_input = QtWidgets.QLineEdit(self) 
        self.manual_bitrate_input.setPlaceholderText("Enter custom bitrate (e.g., 250000)")
        self.manual_bitrate_input.setFixedHeight(30)
        self.manual_bitrate_input.setVisible(False)  
        self.connect_button = QtWidgets.QPushButton("Connect", self)
        self.connect_button.setStyleSheet("background-color: #3498db; color: white; font-weight: bold;")
        self.connect_button.setFixedHeight(30) 
        self.connect_button.clicked.connect(self.on_connect)
        self.disconnect_button = QtWidgets.QPushButton("Disconnect", self)
        self.disconnect_button.setStyleSheet("background-color: grey; color: white; font-weight: bold;")
        self.disconnect_button.setFixedHeight(30) 
        self.disconnect_button.clicked.connect(self.on_disconnect)
        input_layout.addWidget(self.dropdown1)
        input_layout.addWidget(self.dropdown2)
        input_layout.addWidget(self.manual_bitrate_input)  
        input_layout.addWidget(self.connect_button)
        input_layout.addWidget(self.disconnect_button)
        self.heartbeat_label = QtWidgets.QLabel("Note: Connect PCAN Module to the System, Power ON the Sensor, Select Proper Channel and Baudrate then Click on Connect Button", self)
        self.heartbeat_label.setStyleSheet("font-size: 12px; font-weight: bold; color: green;")
        self.heartbeat_label.setVisible(True) 

        main_layout.addWidget(self.heartbeat_label)
        
        main_layout.addLayout(input_layout)
        self.tab_widget = QtWidgets.QTabWidget()
        tab1, tab2, tab3 = QtWidgets.QWidget(), QtWidgets.QWidget(), QtWidgets.QWidget()
        layout1, layout2, layout3 = QtWidgets.QVBoxLayout(), QtWidgets.QVBoxLayout(), QtWidgets.QVBoxLayout()
          # ───────────── PRESSURE TAB ─────────────
        self.table_widget = QtWidgets.QTableWidget(self)
        self.table_widget.setColumnCount(2)
        self.table_widget.setHorizontalHeaderLabels(["CAN ID", "Pressure Value (Units in bar)"])
        self.table_widget.setColumnWidth(1, 200)  # Adjust column width
        self.table_widget.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        layout1.addWidget(self.table_widget)
        self.read_button = QtWidgets.QPushButton("Read", self)
        self.write_button = QtWidgets.QPushButton("Configure", self)
        self.stop_button = QtWidgets.QPushButton("Stop", self)

        # Disable all buttons initially
        self.read_button.setEnabled(False)
        self.write_button.setEnabled(False)
        self.stop_button.setEnabled(False)

        # Set styles for disabled buttons (optional)
        self.read_button.setStyleSheet("background-color: gray; color: white;")
        self.write_button.setStyleSheet("background-color: gray; color: white;")
        self.stop_button.setStyleSheet("background-color: gray; color: white;")

        self.read_button.clicked.connect(self.on_start)
        self.write_button.clicked.connect(self.on_write)
        self.stop_button.clicked.connect(self.on_stop)
        button_layout1 = QtWidgets.QHBoxLayout()
        button_layout1.addWidget(self.read_button)
        button_layout1.addWidget(self.write_button)
        button_layout1.addWidget(self.stop_button)
        layout1.addLayout(button_layout1)
        # ───────────── ROTARY ENCODER TAB ─────────────
        self.table_widget2 = QtWidgets.QTableWidget(self)
        self.table_widget2.setColumnCount(2)
        self.table_widget2.setHorizontalHeaderLabels(["CAN ID", "Angle (°)"])
        layout2.addWidget(self.table_widget2)
        # Buttons for Rotary Encoder Tab
        self.read_button2 = QtWidgets.QPushButton("Read", self)
        self.write_button2 = QtWidgets.QPushButton("Configure", self)
        self.SetZero2 = QtWidgets.QPushButton("SetZero", self)
        self.stop_button2 = QtWidgets.QPushButton("Stop", self)

         # Disable all buttons initially
        self.read_button2.setEnabled(False)
        self.write_button2.setEnabled(False)
        self.SetZero2.setEnabled(False)
        self.stop_button2.setEnabled(False)

        # Set styles for disabled buttons (optional)
        self.read_button2.setStyleSheet("background-color: gray; color: white;")
        self.write_button2.setStyleSheet("background-color: gray; color: white;")
        self.SetZero2.setStyleSheet("background-color: gray; color: white;")
        self.stop_button2.setStyleSheet("background-color: gray; color: white;")

        self.read_button2.clicked.connect(self.start_listening)
        self.write_button2.clicked.connect(self.on_write_tab2)
        self.SetZero2.clicked.connect(self.on_SetZero2)
        self.stop_button2.clicked.connect(self.stop_listening)
        button_layout2 = QtWidgets.QHBoxLayout()
        button_layout2.addWidget(self.read_button2)
        button_layout2.addWidget(self.write_button2)
        button_layout2.addWidget(self.SetZero2)
        button_layout2.addWidget(self.stop_button2)
        layout2.addLayout(button_layout2)
        # ───────────── DIO TAB ─────────────
        self.table_widget3 = QtWidgets.QTableWidget(self)  
        self.table_widget3.setColumnCount(9)
        self.table_widget3.setHorizontalHeaderLabels(["CAN ID","IO-1","IO-2","IO-3","IO-4","IO-5","IO-6","IO-7","IO-8"])
        layout3.addWidget(self.table_widget3)
        # Buttons for DIO Tab
        self.read_button3 = QtWidgets.QPushButton("Read", self)
        self.stop_button3 = QtWidgets.QPushButton("Stop", self)

        # Disable all buttons initially
        self.read_button3.setEnabled(False)    
        self.stop_button3.setEnabled(False)

        # Set styles for disabled buttons (optional)
        self.read_button3.setStyleSheet("background-color: gray; color: white;")
        self.stop_button3.setStyleSheet("background-color: gray; color: white;")

        self.read_button3.clicked.connect(self.on_start_tab3)
        self.stop_button3.clicked.connect(self.on_stop_tab3)

        button_layout3 = QtWidgets.QHBoxLayout()
        button_layout3.addWidget(self.read_button3)
        button_layout3.addWidget(self.stop_button3)
        layout3.addLayout(button_layout3)
        tab1.setLayout(layout1)
        tab2.setLayout(layout2)
        tab3.setLayout(layout3)
        self.tab_widget.addTab(tab1, "Pressure")
        self.tab_widget.addTab(tab2, "Rotary Encoder")
        self.tab_widget.addTab(tab3, "DIO")
        main_layout.addWidget(self.tab_widget)
    def update_datetime(self):
        self.datetime_label.setText(QtCore.QDateTime.currentDateTime().toString("yyyy-MM-dd hh:mm:ss"))
    def populate_can_channels(self):
        """Detects available PCAN channels and populates dropdown1."""
        available_channels = can.detect_available_configs("pcan")
        channel_names = [config["channel"] for config in available_channels]
        if channel_names:
            self.dropdown1.addItem("Select Channel")
            self.dropdown1.addItems(channel_names)
        else:
            self.dropdown1.addItem("No CAN channels found")
    def on_channel_selected(self):
        """Stores the selected channel when user picks from dropdown."""
        self.selected_channel = self.dropdown1.currentText()
        print(f"Selected CAN Channel: {self.selected_channel}")
    def on_bitrate_selected(self):
        """Handles the selection of bitrate from dropdown2."""
        selected = self.dropdown2.currentText()
        if selected == "Manual":
            self.manual_bitrate_input.setVisible(True)  
            self.manual_bitrate_input.setFocus() 
            manual_bitrate = self.manual_bitrate_input.text().strip()
            if manual_bitrate:  
                try:
                    self.selected_bitrate = int(manual_bitrate)  
                    print(f"Selected Bitrate: {self.selected_bitrate}")
                except ValueError:
                    print("Invalid bitrate. Enter a numeric value.")
                    self.selected_bitrate = None  
            else:
                print("No bitrate entered.")
                self.selected_bitrate = None  

        else:
            self.selected_bitrate = selected  
            print(f"Selected Bitrate: {self.selected_bitrate}")
    def show_manual_bitrate_input_dialog(self):
        """Shows the input dialog for manual bitrate."""
        text, ok = QtWidgets.QInputDialog.getText(self, "Enter Bitrate", "Enter the custom bitrate:")

        if ok and text:
            try:
                entered_bitrate = int(text)
                self.selected_bitrate = str(entered_bitrate) 
                QtWidgets.QMessageBox.information(self, "Bitrate Selected", f"Custom Bitrate {self.selected_bitrate} selected successfully.")
            except ValueError:
                QtWidgets.QMessageBox.warning(self, "Invalid Input", "Enter a valid numeric bitrate.")

            self.manual_bitrate_input.setVisible(False)  
        else:
            self.selected_bitrate = None
            self.manual_bitrate_input.setVisible(False)  
            print("Manual bitrate selection canceled.")



    def on_connect(self):
            # Validate that a channel and a bitrate are selected
        if not self.selected_channel or self.selected_channel == "Select Channel":
         QtWidgets.QMessageBox.warning(self, "Selection Error", "Select a valid CAN Channel before Connecting.")
         return
        if not self.selected_bitrate or self.selected_bitrate == "Select Baudrate":
         QtWidgets.QMessageBox.warning(self, "Selection Error", "Select a valid Baudrate before Connecting.")
         return
        self.connect_button.setEnabled(False)
        self.connect_button.setStyleSheet("background-color: grey; color: white; font-weight: bold;")  # Grey when disabled
        self.disconnect_button.setEnabled(True)
        self.disconnect_button.setStyleSheet("background-color: #e74c3c; color: white; font-weight: bold;")  # Red when enabled


        # Change button colors
        QtWidgets.QApplication.processEvents() 
        if self.selected_channel and self.selected_bitrate:
            try:
                print(f"🔗 Connecting to {self.selected_channel} with bitrate {self.selected_bitrate}...")
                self.shared_bus = can.Bus(interface="pcan", channel=self.selected_channel, bitrate=self.selected_bitrate)

                self.heartbeat_thread = HeartbeatThread(self.shared_bus)
                self.heartbeat_thread.heartbeat_signal.connect(self.update_heartbeat_ui)
                self.heartbeat_thread.error_signal.connect(self.show_error_alert) 
                # self.heartbeat_thread.sucess_signal.connect(self.show_sucess_alert)
                self.heartbeat_thread.start()
                self.heartbeat_label.setText("PCAN Initialize Succesfully - Waiting for Heartbeat...")
                self.heartbeat_label.setStyleSheet("font-size: 12px; font-weight: bold; color: green;")
                QtWidgets.QApplication.processEvents()
                self.heartbeat_label.setVisible(True)

            except can.CanError as e:
                print(f"❌ CAN Bus Connection Error: {e}")
                #QtWidgets.QMessageBox.critical(self, "CAN Bus Error", f"Failed to connect: {e}")
                QtWidgets.QMessageBox.critical(self, "CAN Bus Error", f"Failed to connect: Disconnect the Channel and Connect with Proper Baudrate")
                
    def set_all_buttons_enabled(self, enabled):
        """Enable or disable all buttons except 'Connect' and 'Disconnect'."""
        buttons = [
            self.read_button, self.write_button, self.stop_button,
            self.read_button2, self.write_button2, self.stop_button2, self.SetZero2,
            self.read_button3, self.stop_button3
        ]
        for btn in buttons:
            btn.setEnabled(enabled)


    def show_error_alert(self, message):
        """Show a warning message when a CAN bus error occurs."""
        QtWidgets.QMessageBox.warning(self, "Bus Error", message)
    def show_sucess_alert(self, message):
        """Show a warning message when a CAN bus error occurs."""
        QtWidgets.QMessageBox.information(self, "Bus sucess", message)

    def update_heartbeat_ui(self, node_list):
        if node_list:  
            nodes_str = ", ".join(map(str, node_list))  
            self.heartbeat_label.setText(f"Heartbeat Nodes: {nodes_str}")
            self.heartbeat_label.setStyleSheet("font-size: 12px; font-weight: bold; color: green;")

            # Enable Read button, keep Stop disabled for Pressure
            self.read_button.setEnabled(True)
            self.read_button.setStyleSheet("background-color: green; color: white;")

            self.stop_button.setEnabled(False)  # Ensure Stop remains disabled initially
            self.stop_button.setStyleSheet("background-color: grey; color: white;")

              # Enable Read button, keep Stop disabled for Rotary
            self.read_button2.setEnabled(True)
            self.read_button2.setStyleSheet("background-color: green; color: white;")

            self.SetZero2.setEnabled(True)
            self.SetZero2.setStyleSheet("background-color: green; color: white;")

            self.stop_button2.setEnabled(False)  # Ensure Stop remains disabled initially
            self.stop_button2.setStyleSheet("background-color: grey; color: white;")

            # Enable Read button, keep Stop disabled for DIO
            self.read_button3.setEnabled(True)
            self.read_button3.setStyleSheet("background-color: green; color: white;")

            self.stop_button3.setEnabled(False)  # Ensure Stop remains disabled initially
            self.stop_button3.setStyleSheet("background-color: grey; color: white;")

            if hasattr(self, 'heartbeat_thread') and self.heartbeat_thread and self.heartbeat_thread.isRunning():
                # self.heartbeat_thread.stop()
             self.heartbeat_thread.stop()
             self.heartbeat_thread.wait()  # Ensure proper stopping
             self.heartbeat_thread = None
        else:
            self.set_all_buttons_enabled(False)
            self.heartbeat_label.setText("Heartbeat: ❌ No Active Nodes")
            self.heartbeat_label.setStyleSheet("font-size: 12px; font-weight: bold; color: red;")

    def on_disconnect(self):
        self.connect_button.setEnabled(True)
        self.connect_button.setStyleSheet("background-color: #3498db; color: white; font-weight: bold;")  # Sky blue when enabled

        self.disconnect_button.setEnabled(False)  # Ensure it's disabled initially
        self.disconnect_button.setStyleSheet("background-color: grey; color: white; font-weight: bold;")  # Grey when disabled

        """Safely disconnect from the CAN bus and stop all related threads."""
        if hasattr(self, 'heartbeat_thread') and self.heartbeat_thread is not None:
            print("🛑 Stopping Heartbeat Thread...")
            self.heartbeat_thread.stop()  
            self.heartbeat_thread.wait()  
            self.heartbeat_thread = None  

        if hasattr(self, 'shared_bus') and self.shared_bus is not None:
            try:
                print("🔌 Disconnecting CAN Bus...")
                self.shared_bus.shutdown() 
                self.shared_bus = None 
                QtWidgets.QMessageBox.information(self, "Disconnected", "CAN Bus has been Disconnected Successfully.")
                self.heartbeat_label.setText("PCAN Uninitialize Succesfully")
                self.heartbeat_label.setStyleSheet("font-size: 12px; font-weight: bold; color: green;")

            except Exception as e:
                print(f"⚠️ Error while disconnecting CAN Bus: {e}")
                QtWidgets.QMessageBox.warning(self, "Disconnection Error", f"Error while disconnecting: {e}")
        self.current_node_ids = {}   
        self.current_node_ids_rottery = {}          
        self.table_widget.setRowCount(0)   
        self.table_widget2.setRowCount(0)   
        self.table_widget3.setRowCount(0)  

    def on_read(self, canid, raw_data, pressure_value): 
        # print(f"Received CAN data - ID: {canid}, Raw Data: {raw_data}, Pressure: {pressure_value}")
        for row in range(self.table_widget.rowCount()):
            existing_id = self.table_widget.item(row, 0)
            if existing_id and existing_id.text() == str(canid):
                self.table_widget.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{pressure_value:.2f} bar"))
                count_item = self.table_widget.item(row, 3)
                if count_item:
                    current_count = int(count_item.text()) + 1
                return 
        row_position = self.table_widget.rowCount()  
        self.table_widget.insertRow(row_position)
        self.table_widget.setItem(row_position, 0, QtWidgets.QTableWidgetItem(str(canid)))   # CAN ID
        self.table_widget.setItem(row_position, 1, QtWidgets.QTableWidgetItem(f"{pressure_value:.2f} bar")) # Pressure      

    def on_start(self): 
        print("Starting Pressure...")  
        self.read_button.setEnabled(False)  
        self.stop_button.setEnabled(True) 
        self.write_button.setEnabled(False) 
        self.tab_widget.setTabEnabled(0, True)
        self.tab_widget.setTabEnabled(1, False)
        self.tab_widget.setTabEnabled(2, False)
         
        self.read_button.setStyleSheet("background-color: gray; color: white;")
        self.stop_button.setStyleSheet("background-color: green; color: white;")
        self.write_button.setStyleSheet("background-color: gray; color: white;")   
           
        print(f"Connecting to {self.selected_channel} with bitrate {self.selected_bitrate}...")
        global active_nodes  
        print(f"🚀 Starting CANReaderThread with nodes: {active_nodes}")
        self.shared_bus = can.Bus(interface="pcan", channel=self.selected_channel, bitrate=self.selected_bitrate)
        self.can_thread = CANReaderThread(self.shared_bus, active_nodes)
        self.can_thread.data_received.connect(self.on_read) 
        self.can_thread.node_id_signal.connect(self.update_last_node_id)  
        self.can_thread.start()
        print("self.can_thread_pressure", self.can_thread)
        print("Is CAN thread running?", self.can_thread.isRunning())
        print("Current Node IDs:", list(active_nodes))

        if self.can_thread and active_nodes:
            node_ids = list(active_nodes)
            self.request_thread_pressure = CANRequestSenderpressure(self.can_thread.bus, node_ids)
            self.can_thread.error_detected.connect(self.request_thread_pressure.handle_error)
            self.request_thread_pressure.start()
            print("🚀 CAN message sending started.") 
          

    def update_last_node_id(self, node_id):
        """✅ Updates the dictionary to track multiple active nodes."""
        if not hasattr(self, 'current_node_ids'):
            self.current_node_ids = {}  
        self.current_node_ids[node_id] = True
        print(f"🌟 Active Node IDs updated: {list(self.current_node_ids.keys())}")

    def on_stop(self):
        self.read_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.write_button.setEnabled(True)
        self.tab_widget.setTabEnabled(0, True)
        self.tab_widget.setTabEnabled(1, True)
        self.tab_widget.setTabEnabled(2, True)
        self.read_button.setStyleSheet("background-color: green; color: white;")
        self.stop_button.setStyleSheet("")
        self.write_button.setStyleSheet("background-color: green; color: white;")
        print("Stopping pressure...")

        """Stop CAN data reading."""
       # Stop the periodic CAN request thread if it exists
        if hasattr(self, "request_thread_pressure") and self.request_thread_pressure:
            self.request_thread_pressure.stop()
            self.request_thread_pressure = None
            print("🛑 Stopped periodic CAN requests.")
       
        if self.can_thread and self.can_thread.isRunning():
            self.can_thread.stop()
            self.can_thread.wait()  # Ensure the thread properly stops
            self.can_thread = None
            QtWidgets.QMessageBox.information(self, "Stopped", "CAN Data Reading Stopped.")
        
    def on_read_tab2(self, canid, angle): 
        print("Reading Encoder...")
        """Updates the UI table with the latest CAN ID and angle data."""
        for row in range(self.table_widget2.rowCount()):
            if self.table_widget2.item(row, 0) and self.table_widget2.item(row, 0).text() == canid:
                self.table_widget2.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{angle:.2f} °"))
                return  
        row_position = self.table_widget2.rowCount()
        self.table_widget2.insertRow(row_position)
        
        self.table_widget2.setItem(row_position, 0, QtWidgets.QTableWidgetItem(canid))
        self.table_widget2.setItem(row_position, 1, QtWidgets.QTableWidgetItem(f"{angle:.2f} °"))
    def on_SetZero2(self):
        """Calls the set zero function inside the CAN request thread."""
        if self.request_thread_rottery:
            self.request_thread_rottery.on_setzero()  # Trigger the set zero command
            print("🔘 Set Zero command triggered.")
    def on_write_tab2(self):
        """Main popup with two options: Configure Node ID or Configure Baudrate"""
        print("Writing Rotary...")
        if not hasattr(self, 'current_node_ids_rottery') or not self.current_node_ids_rottery:
            QtWidgets.QMessageBox.warning(self, "Error", "No active nodes detected!")
            return
            
        # print(f"🔄 Active Nodes for Configuration: {list(self.current_node_ids.keys())}")
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("CAN Node ID and Baudrate Configure")
        dialog.setFixedSize(300, 200)
        layout = QtWidgets.QVBoxLayout()
        node_id_button = QtWidgets.QPushButton("Configure Node ID")
        node_id_button.clicked.connect(lambda: self.configure_node_id(dialog))
        baudrate_button = QtWidgets.QPushButton("Configure Baudrate")
        baudrate_button.clicked.connect(lambda: self.configure_baudrate(dialog))
        layout.addWidget(node_id_button)
        layout.addWidget(baudrate_button)
        dialog.setLayout(layout)
        dialog.exec_()
    def configure_node_id(self, parent):
        """Popup to select Node ID"""

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("CAN Node ID Configurator")
        dialog.setFixedSize(300, 200)
        layout = QtWidgets.QVBoxLayout(dialog)
        self.prev_label = QtWidgets.QLabel("Current Node ID:")
        self.prev_input = QtWidgets.QComboBox()
        self.prev_input.addItem("Select Node ID")
        if hasattr(self, 'current_node_ids_rottery') and self.current_node_ids_rottery:
            for node_id in self.current_node_ids_rottery.keys():
                self.prev_input.addItem(str(node_id))
        self.new_label = QtWidgets.QLabel("Replace ID:")
        self.new_input = QtWidgets.QLineEdit()
        button_layout = QtWidgets.QHBoxLayout()
        self.ok_buttonrot = QtWidgets.QPushButton("OK")
        self.cancel_buttonrot = QtWidgets.QPushButton("Cancel")
        button_layout.addWidget(self.ok_buttonrot)
        button_layout.addWidget(self.cancel_buttonrot)
        layout.addWidget(self.prev_label)
        layout.addWidget(self.prev_input)
        layout.addWidget(self.new_label)
        layout.addWidget(self.new_input)
        layout.addLayout(button_layout)
        dialog.setLayout(layout) 
        self.cancel_buttonrot.clicked.connect(dialog.reject)  
        self.ok_buttonrot.clicked.connect(lambda: self.on_okrot(dialog, self.prev_input.currentText()))  
        dialog.exec_()  
    def on_okrot(self, dialog,selected_prev_node_id):
        prev_node_id = int(selected_prev_node_id)
        new_node_id = int(self.new_input.text())
        if prev_node_id is None:
            QtWidgets.QMessageBox.warning(self, "Error", "No active CAN Node detected!")
            return
        QtWidgets.QMessageBox.information(self, "Configuration", f"Changing Node ID from {prev_node_id} to {new_node_id}...")
        self.execute_can_operationsrot(prev_node_id, new_node_id)
        dialog.accept()
    def execute_can_operationsrot(self, current_id, new_id):
        """ Execute CAN operations step-by-step with a progress bar """
        progress_dialog = QtWidgets.QProgressDialog(self)
        progress_dialog.setWindowTitle("Processing")
        progress_dialog.setLabelText("Starting CAN operations...")
        progress_dialog.setCancelButton(None) 
        progress_dialog.setMinimum(0)
        progress_dialog.setMaximum(100)
        progress_dialog.setWindowModality(QtCore.Qt.WindowModal)
        progress_dialog.show()

        try:
            progress_dialog.setLabelText("Switching to pre-operational mode...")
            switch_to_pre_operationalrot(current_id, self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(20)
            QtWidgets.QApplication.processEvents()
            progress_dialog.setLabelText(f"Changing Node ID from {current_id} to {new_id}...")
            new_id = new_id -1
            change_node_idrot(self, current_id, new_id, self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(40)
            QtWidgets.QApplication.processEvents()
            progress_dialog.setLabelText("Waiting for changes to take effect...")
            time.sleep(2)
            progress_dialog.setValue(50)
            QtWidgets.QApplication.processEvents()

            progress_dialog.setLabelText("Saving parameters...")

            save_parametersrot(self,current_id,self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(70)
            QtWidgets.QApplication.processEvents()

            reset_required = QtWidgets.QMessageBox.question(self, "Reset Required?", "Reset Node?",
                                                            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)


            progress_dialog.setValue(100)
            QtWidgets.QApplication.processEvents()
            QtWidgets.QMessageBox.information(self, "Process Completed", "CAN Node ID Change Successful!")
            QtWidgets.QMessageBox.information(self, "IMPORTANCE", "Disconnect bus and again connect bus and sensor")

        finally:
            progress_dialog.close()

    def configure_baudrate(self, parent):
        """Popup to select Node ID"""

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("CAN baudrate ID Configurator")
        dialog.setFixedSize(300, 200)
        layout = QtWidgets.QVBoxLayout(dialog)
        self.prev_label = QtWidgets.QLabel("Current Node ID:")
        self.prev_input = QtWidgets.QComboBox()
        self.prev_input.addItem("Select Node ID")
        if hasattr(self, 'current_node_ids_rottery') and self.current_node_ids_rottery:
            for node_id in self.current_node_ids_rottery.keys():
                self.prev_input.addItem(str(node_id))
        self.new_label = QtWidgets.QLabel("Replace  Baudrate:")
        self.new_input = self.new_input = QtWidgets.QComboBox()
        baudrate_options = {
            "20 kbit/s": "00",
            "50 kbit/s": "01",
            "100 kbit/s": "02",
            "125 kbit/s": "03",
            "250 kbit/s": "04",
            "500 kbit/s": "05",
            "800 kbit/s": "06",
            "1000 kbit/s": "07"
        }

        for label, value in baudrate_options.items():
            self.new_input.addItem(label, value)
        button_layout = QtWidgets.QHBoxLayout()
        self.ok_buttonrotb = QtWidgets.QPushButton("OK")
        self.cancel_buttonrotb = QtWidgets.QPushButton("Cancel")
        button_layout.addWidget(self.ok_buttonrotb)
        button_layout.addWidget(self.cancel_buttonrotb)
        layout.addWidget(self.prev_label)
        layout.addWidget(self.prev_input)
        layout.addWidget(self.new_label)
        layout.addWidget(self.new_input)
        layout.addLayout(button_layout)
        dialog.setLayout(layout) 
        self.cancel_buttonrotb.clicked.connect(dialog.reject)  
        self.ok_buttonrotb.clicked.connect(lambda: self.on_okrotb(dialog, self.prev_input.currentText()))  
        dialog.exec_()  
    def on_okrotb(self, dialog,selected_prev_node_id):
        prev_node_id = int(selected_prev_node_id)
        # new_node_id = int(self.new_input.text())
        new_node_id = new_node_id = self.new_input.currentData()
        print(f"Selected Baudrate: {new_node_id}h")
        if prev_node_id is None:
            QtWidgets.QMessageBox.warning(self, "Error", "No active CAN Node detected!")
            return
        QtWidgets.QMessageBox.information(self, "Configuration", f"Changing  Baudrate")
        self.execute_can_operationsrotb(prev_node_id, new_node_id)
        dialog.accept()
    def execute_can_operationsrotb(self, current_id, new_id):
        """ Execute CAN operations step-by-step with a progress bar """

        progress_dialog = QtWidgets.QProgressDialog(self)
        progress_dialog.setWindowTitle("Processing")
        progress_dialog.setLabelText("Starting CAN operations...")
        progress_dialog.setCancelButton(None)  
        progress_dialog.setMinimum(0)
        progress_dialog.setMaximum(100)
        progress_dialog.setWindowModality(QtCore.Qt.WindowModal)
        progress_dialog.show()

        try:

            progress_dialog.setLabelText("Switching to pre-operational mode...")
            switch_to_pre_operationalrotb(current_id, self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(20)
            QtWidgets.QApplication.processEvents()


            progress_dialog.setLabelText(f"Changing Node ID from {current_id} to {new_id}...")

            change_node_idrotb(self, current_id, new_id, self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(40)
            QtWidgets.QApplication.processEvents()

            progress_dialog.setLabelText("Waiting for changes to take effect...")
            time.sleep(2)
            progress_dialog.setValue(50)
            QtWidgets.QApplication.processEvents()


            progress_dialog.setLabelText("Saving parameters...")
            
            save_parametersrotb(self,current_id,self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(70)
            QtWidgets.QApplication.processEvents()

          
            reset_required = QtWidgets.QMessageBox.question(self, "Reset Required?", "Reset Node?",
                                                            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)


            progress_dialog.setValue(100)
            QtWidgets.QApplication.processEvents()
            QtWidgets.QMessageBox.information(self, "Process Completed", "CAN baudrate Change Successful!")
            QtWidgets.QMessageBox.information(self, "IMPORTANCE", "Disconnect bus and again connect bus and sensor")

        finally:
            progress_dialog.close()

        
    def start_listening(self):
        self.read_button2.setEnabled(False)
        self.stop_button2.setEnabled(True)
        self.write_button2.setEnabled(False)
        self.tab_widget.setTabEnabled(0, False)
        self.tab_widget.setTabEnabled(1, True)
        self.tab_widget.setTabEnabled(2, False)
        self.read_button2.setStyleSheet("background-color: gray; color: white;")
        self.stop_button2.setStyleSheet("background-color:green; color: white;")
        self.write_button2.setStyleSheet("background-color: gray; color: white;")
        print("Starting Roatry...")        
        print(f"Connecting to {self.selected_channel} with bitrate {self.selected_bitrate}...")
        """Start CAN data reading in a separate thread."""
        global active_nodes 
        print(f"🚀 Starting CANReaderThread with nodes: {active_nodes }")
        self.shared_bus = can.Bus(interface="pcan", channel=self.selected_channel, bitrate=self.selected_bitrate)
        self.can_listener = CANListenerRotary(self.shared_bus, active_nodes )
        self.can_listener.angle_updated.connect(self.on_read_tab2) 
        self.can_listener.node_id_signal_rotarry.connect(self.update_last_node_id1) 
        self.can_listener.start()
        print("self.can_thread_rottery", self.can_listener)
        print("Is CAN thread running?", self.can_listener.isRunning())
        print("Current Node IDs:", list(active_nodes))
        if self.can_listener and active_nodes:
            node_ids = list(active_nodes)
            self.request_thread_rottery = CANRequestSenderRotary(self.can_listener.bus_rotary, node_ids)
            self.can_listener.error_detected_rotarry.connect(self.request_thread_rottery.handle_error)
            self.request_thread_rottery.start()
            print("🚀 CAN message sending started.") 
    def update_last_node_id1(self, node_id):
        """✅ Updates the dictionary to track multiple active nodes."""
        if not hasattr(self, 'current_node_ids_rottery'):
            self.current_node_ids_rottery = {}
        self.current_node_ids_rottery[node_id] = True 
        print(f"🌟 Active Node IDs updated: {list(self.current_node_ids_rottery.keys())}")

    def stop_listening(self):
        self.read_button2.setEnabled(True)
        self.stop_button2.setEnabled(False)
        self.write_button2.setEnabled(True)
        self.tab_widget.setTabEnabled(0, True)
        self.tab_widget.setTabEnabled(1, True)
        self.tab_widget.setTabEnabled(2, True)

        self.read_button2.setStyleSheet("background-color: green; color: white;")
        self.stop_button2.setStyleSheet("")
        self.write_button2.setStyleSheet("background-color: green; color: white;")
            
        print("Stopping roatry...")
        """Stop CAN data reading."""
        if self.request_thread_rottery:
            self.request_thread_rottery.stop()
            self.request_thread_rottery = None
            print("🛑 Stopped periodic CAN requests.")
       
        if self.can_listener.isRunning():
            self.can_listener.stop()
            self.can_listener = None
            QtWidgets.QMessageBox.information(self, "Stopped", "CAN data reading stopped.")

    def on_read_tab3(self, canid, binary_representation):
        print("Reading DIO...")
        print(f"Received CAN data - ID: {canid}, Data: {binary_representation}")
        if not hasattr(self, 'can_ids'):
            self.can_ids = set()
        canid_str = str(canid)
        canid_found = False
        for row in range(self.table_widget3.rowCount()):
            if self.table_widget3.item(row, 0) and self.table_widget3.item(row, 0).text() == canid_str:
                for i in range(8):
                    state = "ON" if binary_representation[i] == '1' else "OFF"
                    self.table_widget3.setItem(row, i + 1, QtWidgets.QTableWidgetItem(state))
                canid_found = True
                break

        if not canid_found:

            row_position = self.table_widget3.rowCount()
            self.table_widget3.insertRow(row_position)
            self.table_widget3.setItem(row_position, 0, QtWidgets.QTableWidgetItem(canid_str))

            for i in range(8):
                state = "ON" if binary_representation[i] == '1' else "OFF"
                self.table_widget3.setItem(row_position, i + 1, QtWidgets.QTableWidgetItem(state))

            self.can_ids.add(canid_str)

        print(f"🔄 Updated table with DIO Status: {binary_representation}")


    def on_start_tab3(self): 
        self.read_button3.setEnabled(False)
        self.stop_button3.setEnabled(True)
        self.tab_widget.setTabEnabled(0, False)
        self.tab_widget.setTabEnabled(1, False)
        self.tab_widget.setTabEnabled(2, True)
        self.stop_button3.setStyleSheet("background-color: green; color: white;")
        self.read_button3.setStyleSheet("background-color: gray; color: white;")
        self.stop_button3.setStyleSheet("background-color: green; color: white;")
        print("Starting DIO...")        
        print(f"Connecting to {self.selected_channel} with bitrate {self.selected_bitrate}...")
        """Start CAN data reading in a separate thread."""
        global active_nodes 
        print(f"🚀 Starting CANReaderThread with nodes: {active_nodes }")
        self.shared_bus = can.Bus(interface="pcan", channel=self.selected_channel, bitrate=self.selected_bitrate)
        self.can_thread_DIO = CANReaderThreadDIO(self.shared_bus, active_nodes )
        self.can_thread_DIO.data_received_dio.connect(self.on_read_tab3)
        self.can_thread_DIO.start()
        print("self.can_thread_DIO", self.can_thread_DIO)
        print("Is CAN thread running?", self.can_thread_DIO.isRunning())
        print("Current Node IDs (DIO):", list(active_nodes))

        if self.can_thread_DIO and active_nodes:
            node_ids = list(active_nodes)
            print("self.can_thread_DIO.busdio",self.can_thread_DIO.busdio)
            self.request_thread = CANRequestSender(self.can_thread_DIO.busdio, node_ids)
            self.can_thread_DIO.error_detected_dio.connect(self.request_thread.handle_error)

            self.request_thread.start()
            print("🚀 CAN message sending started.")
   
    def on_stop_tab3(self): 
        self.read_button3.setEnabled(True)
        self.stop_button3.setEnabled(False)
        self.tab_widget.setTabEnabled(0, True)
        self.tab_widget.setTabEnabled(1, True)
        self.tab_widget.setTabEnabled(2, True)
        self.read_button3.setStyleSheet("background-color: green; color: white;")
        self.stop_button3.setStyleSheet("")
        print("Stopping DIO...")
        """Stop CAN data reading."""
        if self.request_thread:
            self.request_thread.stop()
            self.request_thread = None
            print("🛑 Stopped periodic CAN requests.")       
        if self.can_thread_DIO.isRunning():
            self.can_thread_DIO.stop()
            self.can_thread_DIO = None
            QtWidgets.QMessageBox.information(self, "Stopped", "CAN data reading stopped.")
   
        
    def on_write(self): 
        print("Writing Pressure...")
        """Main popup with two options: Configure Node ID or Configure Baudrate"""
        if not hasattr(self, 'current_node_ids') or not self.current_node_ids:
                QtWidgets.QMessageBox.warning(self, "Error", "No active nodes detected!")
                return
            
        # print(f"🔄 Active Nodes for Configuration: {list(self.current_node_ids.keys())}")
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("CAN Node ID and Baudrate Configure")
        dialog.setFixedSize(300, 200)
        layout = QtWidgets.QVBoxLayout()
        node_id_button = QtWidgets.QPushButton("Configure Node ID")
        node_id_button.clicked.connect(lambda: self.configure_node_idp(dialog))
        baudrate_button = QtWidgets.QPushButton("Configure Baudrate")
        baudrate_button.clicked.connect(lambda: self.configure_baudratep(dialog))
        layout.addWidget(node_id_button)
        layout.addWidget(baudrate_button)
        dialog.setLayout(layout)
        dialog.exec_()
    def configure_node_idp(self, parent):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("CAN Node ID Configurator")
        dialog.setFixedSize(300, 200)
        layout = QtWidgets.QVBoxLayout(dialog)
        self.prev_label = QtWidgets.QLabel("Current Node ID:")
        self.prev_input = QtWidgets.QComboBox()
        self.prev_input.addItem("Select Node ID")
        if hasattr(self, 'current_node_ids') and self.current_node_ids:
            for node_id in self.current_node_ids.keys():
                self.prev_input.addItem(str(node_id))
        self.new_label = QtWidgets.QLabel("Replace Node ID:")
        self.new_input = QtWidgets.QLineEdit()

        # Buttons
        button_layout = QtWidgets.QHBoxLayout()
        self.ok_button = QtWidgets.QPushButton("OK")
        self.cancel_button = QtWidgets.QPushButton("Cancel")

        button_layout.addWidget(self.ok_button)
        button_layout.addWidget(self.cancel_button)

        # Add widgets to layout
        layout.addWidget(self.prev_label)
        layout.addWidget(self.prev_input)
        layout.addWidget(self.new_label)
        layout.addWidget(self.new_input)
        layout.addLayout(button_layout)

        dialog.setLayout(layout)

        # Connect buttons
        self.cancel_button.clicked.connect(dialog.reject)
        self.ok_button.clicked.connect(lambda: self.on_ok(dialog, self.prev_input.currentText(), self.new_input.text()))

        # Show dialog
        dialog.exec_()  
    def on_ok(self, dialog, selected_prev_node_id, new_node_id):
        prev_node_id = int(selected_prev_node_id)
        new_node_id = int(new_node_id)
        if prev_node_id is None:
            QtWidgets.QMessageBox.warning(self, "Error", "No active CAN Node detected!")
            return
        QtWidgets.QMessageBox.information(self, "Configuration", f"Changing Node ID from {prev_node_id} to {new_node_id}...")
        self.execute_can_operations(prev_node_id, new_node_id)
        dialog.accept()
    def execute_can_operations(self, current_id, new_id):
        """ Execute CAN operations step-by-step with a progress bar """


        progress_dialog = QtWidgets.QProgressDialog(self)
        progress_dialog.setWindowTitle("Processing")
        progress_dialog.setLabelText("Starting CAN operations...")
        progress_dialog.setCancelButton(None) 
        progress_dialog.setMinimum(0)
        progress_dialog.setMaximum(100)
        progress_dialog.setWindowModality(QtCore.Qt.WindowModal)
        progress_dialog.show()

        try:

            progress_dialog.setLabelText("Switching to pre-operational mode...")
            switch_to_pre_operational(current_id, self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(20)
            QtWidgets.QApplication.processEvents()

            progress_dialog.setLabelText(f"Changing Node ID from {current_id} to {new_id}...")
            change_node_id(self, current_id, new_id, self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(40)
            QtWidgets.QApplication.processEvents()


            progress_dialog.setLabelText("Waiting for changes to take effect...")
            time.sleep(2)
            progress_dialog.setValue(50)
            QtWidgets.QApplication.processEvents()


            progress_dialog.setLabelText("Saving parameters...")
            save_parameters(self, new_id, self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(70)
            QtWidgets.QApplication.processEvents()

            
            reset_required = QtWidgets.QMessageBox.question(self, "Reset Required?", "Reset Node?",
                                                            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)

            if reset_required == QtWidgets.QMessageBox.Yes:
                progress_dialog.setLabelText("Resetting node...")
                reset_node(self, new_id, self.selected_channel, self.selected_bitrate)
                time.sleep(3)
                progress_dialog.setValue(90)
                QtWidgets.QApplication.processEvents()

            progress_dialog.setLabelText("Scanning nodes...")
            scan_nodes(self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(100)
            QtWidgets.QApplication.processEvents()

            QtWidgets.QMessageBox.information(self, "Process Completed", "CAN Node ID Change Successful!")
            QtWidgets.QMessageBox.information(self, "IMPORTANCE", "Disconnect bus and again connect bus and sensor")

        finally:
            progress_dialog.close()
    def configure_baudratep(self, parent):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("CAN Node ID Configurator")
        dialog.setFixedSize(300, 200)
        layout = QtWidgets.QVBoxLayout(dialog)
        self.prev_label = QtWidgets.QLabel("Current Node ID:")
        self.prev_input = QtWidgets.QComboBox()
        self.prev_input.addItem("Select Node ID")
        if hasattr(self, 'current_node_ids') and self.current_node_ids:
            for node_id in self.current_node_ids.keys():
                self.prev_input.addItem(str(node_id))
        self.new_label = QtWidgets.QLabel("Replace  Baudrate:")
        self.new_input = self.new_input = QtWidgets.QComboBox()
        baudrate_options = {
            "1Mbit/s": "00",
            "800kbit/s": "01",
            "500kbit/s": "02",
            "250 kbit/s": "03",
            "125 kbit/s": "04",
            "100 kbit/s": "05",
            "50 kbit/s": "06",
            "20 kbit/s": "07"
            
        }
        for label, value in baudrate_options.items():
            self.new_input.addItem(label, value)
        button_layout = QtWidgets.QHBoxLayout()
        button_layout = QtWidgets.QHBoxLayout()
        self.ok_buttonp = QtWidgets.QPushButton("OK")
        self.cancel_buttonp = QtWidgets.QPushButton("Cancel")
        button_layout.addWidget(self.ok_buttonp)
        button_layout.addWidget(self.cancel_buttonp)
        layout.addWidget(self.prev_label)
        layout.addWidget(self.prev_input)
        layout.addWidget(self.new_label)
        layout.addWidget(self.new_input)
        layout.addLayout(button_layout)
        dialog.setLayout(layout) 
        self.cancel_buttonp.clicked.connect(dialog.reject)  
        self.ok_buttonp.clicked.connect(lambda: self.on_okp(dialog,self.prev_input.currentText())  )
        dialog.exec_()  
    def on_okp(self, dialog, selected_prev_node_id):
        prev_node_id = int(selected_prev_node_id)

        new_node_id = new_node_id = self.new_input.currentData()
        if prev_node_id is None:
            QtWidgets.QMessageBox.warning(self, "Error", "No active CAN Node detected!")
            return
        QtWidgets.QMessageBox.information(self, "Configuration", f"Changing Node ID from {prev_node_id} to {new_node_id}...")
        self.execute_can_operationsp(prev_node_id, new_node_id)
        dialog.accept()
    def execute_can_operationsp(self, current_id, new_id):
        """ Execute CAN operations step-by-step with a progress bar """


        progress_dialog = QtWidgets.QProgressDialog(self)
        progress_dialog.setWindowTitle("Processing")
        progress_dialog.setLabelText("Starting CAN operations...")
        progress_dialog.setCancelButton(None)  
        progress_dialog.setMinimum(0)
        progress_dialog.setMaximum(100)
        progress_dialog.setWindowModality(QtCore.Qt.WindowModal)
        progress_dialog.show()

        try:
            progress_dialog.setLabelText("Switching to pre-operational mode...")
            switch_to_pre_operationalp(current_id, self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(20)
            QtWidgets.QApplication.processEvents()
            progress_dialog.setLabelText(f"Changing Node ID from {current_id} to {new_id}...")
            change_node_idp(self, current_id, new_id, self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(40)
            QtWidgets.QApplication.processEvents()
            progress_dialog.setLabelText("Waiting for changes to take effect...")
            time.sleep(2)
            progress_dialog.setValue(50)
            QtWidgets.QApplication.processEvents()
            progress_dialog.setLabelText("Saving parameters...")
            save_parametersp(self, new_id, self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(70)
            QtWidgets.QApplication.processEvents()
            reset_required = QtWidgets.QMessageBox.question(self, "Reset Required?", "Reset Node?",
                                                            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)

            if reset_required == QtWidgets.QMessageBox.Yes:
                progress_dialog.setLabelText("Resetting node...")
                reset_nodep(self, new_id, self.selected_channel, self.selected_bitrate)
                time.sleep(3)
                progress_dialog.setValue(90)
                QtWidgets.QApplication.processEvents()
            progress_dialog.setLabelText("Scanning nodes...")
            scan_nodesp(self.selected_channel, self.selected_bitrate)
            progress_dialog.setValue(100)
            QtWidgets.QApplication.processEvents()
            QtWidgets.QMessageBox.information(self, "Process Completed", "CAN Baudrate Change Successful!")
            QtWidgets.QMessageBox.information(self, "IMPORTANCE", "Disconnect bus and again connect bus and sensor")

        finally:
            progress_dialog.close()
def switch_to_pre_operational(node_id,channel, bitrate,):
    channel1 = channel
    print("channel1 =",channel1)
    baudrate1 = bitrate
    print("baudrate1 =",baudrate1) 
    bus = can.Bus(interface="pcan", channel=channel1, bitrate=baudrate1)
    msg_id = 0x00
    data = [0x80, node_id]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"🔄 Node {node_id} switched to Pre-Operational Mode.")
    except can.CanError as e:
        print(f"❌ Failed4: {e}")
    bus.shutdown()


def change_node_id(self,current_node_id, new_node_id,channel, bitrate,):
    channel2 = channel
    print("channel2 =",channel2)
    baudrate2 = bitrate
    print("baudrate2 =",baudrate2)
    bus = can.Bus(interface="pcan", channel=channel2, bitrate=baudrate2)
    time.sleep(3)
    msg_id = 0x600 + current_node_id
    data = [0x22, 0x20, 0x23, 0x00, new_node_id, 0x00, 0x00, 0x00]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"✅ Node ID change request sent from {current_node_id} to {new_node_id}.")
        response = bus.recv(timeout=1)
        if response and response.arbitration_id == (0x700 + current_node_id):
            print(f"✅ Device acknowledged: {list(response.data)}")
        else:
            print("⚠️ No acknowledgment.")
    except can.CanError as e:
        print(f"❌ Failed 3: {e}")
    bus.shutdown()


def save_parameters(self,node_id,channel, bitrate,):
    channel3 = channel
    print("channel3 =",channel3)
    baudrate3 = bitrate
    print("baudrate3 =",baudrate3)
    bus = can.Bus(interface="pcan", channel=channel3, bitrate=baudrate3)
    msg_id = 0x600 + node_id
    data = [0x22, 0x10, 0x10, 0x01, 0x73, 0x61, 0x76, 0x65]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"💾 Save command sent to Node {node_id}.")
    except can.CanError as e:
        print(f"❌ Failed 2: {e}")
    bus.shutdown()


def reset_node(self,node_id,channel, bitrate,):
    channel4 = channel
    print("channel4 =",channel)
    baudrate4 = bitrate
    print("baudrate4 =",baudrate4)
    bus = can.Bus(interface="pcan", channel=channel4, bitrate=baudrate4)
    msg_id = 0x00
    data = [0x81, node_id]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"🔄 Reset command sent to Node {node_id}. Restarting...")
    except can.CanError as e:
        print(f"❌ Failed 1: {e}")
    bus.shutdown()


def scan_nodes(channel, bitrate,):
    channel5 = channel
    print("channe5 =",channel)
    baudrate5 = bitrate
    print("baudrate5 =",baudrate5)
    bus = can.Bus(interface="pcan", channel=channel5, bitrate=baudrate5)
    print("🔍 Scanning CANopen nodes...")

    found_nodes = []
    for node_id in range(1, 128):
        msg_id = 0x600 + node_id
        data = [0x40, 0x18, 0x10, 0x04, 0x00, 0x00, 0x00, 0x00]
        message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

        try:
            bus.send(message)
            response = bus.recv(timeout=0.5)
            if response and response.arbitration_id == (0x580 + node_id):
                print(f"✅ Active Node Found: {node_id}, Response: {list(response.data)}")
                found_nodes.append(node_id)
        except can.CanError:
            pass

    if not found_nodes:
        print("⚠️ No active nodes found!")
    print("🔍 Scan complete.")
    bus.shutdown()
def switch_to_pre_operationalp(node_id,channel, bitrate,):
    channel1 = channel
    print("channel1 =",channel1)
    baudrate1 = bitrate
    node_id = int(node_id)
    print("baudrate1 =",baudrate1) 
    bus = can.Bus(interface="pcan", channel=channel1, bitrate=baudrate1)
    msg_id = 0x00
    data = [0x80, node_id]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"🔄 Node {node_id} switched to Pre-Operational Mode.")
    except can.CanError as e:
        print(f"❌ Failed4: {e}")
    bus.shutdown()


def change_node_idp(self,current_node_id, new_node_id,channel, bitrate,):
    channel2 = channel
    print("channel2 =",channel2)
    baudrate2 = bitrate
    print("baudrate2 =",baudrate2)
    new_node_id = int(new_node_id)
    bus = can.Bus(interface="pcan", channel=channel2, bitrate=baudrate2)
    time.sleep(3)
    msg_id = 0x600 + current_node_id
    data = [0x22, 0x21, 0x23, 0x00, new_node_id, 0x00, 0x00, 0x00]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"✅ Node ID change request sent from {current_node_id} to {new_node_id}.")
        response = bus.recv(timeout=1)
        if response and response.arbitration_id == (0x700 + current_node_id):
            print(f"✅ Device acknowledged: {list(response.data)}")
        else:
            print("⚠️ No acknowledgment.")
    except can.CanError as e:
        print(f"❌ Failed 3: {e}")
    bus.shutdown()
def save_parametersp(self,node_id,channel, bitrate,):
    channel3 = channel
    node_id = int(node_id)
    print("channel3 =",channel3)
    baudrate3 = bitrate
    print("baudrate3 =",baudrate3)
    bus = can.Bus(interface="pcan", channel=channel3, bitrate=baudrate3)
    msg_id = 0x600 + node_id
    data = [0x22, 0x10, 0x10, 0x01, 0x73, 0x61, 0x76, 0x65]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"💾 Save command sent to Node {node_id}.")
    except can.CanError as e:
        print(f"❌ Failed 2: {e}")
    bus.shutdown()


def reset_nodep(self,node_id,channel, bitrate,):
    channel4 = channel
    node_id = int(node_id)
    print("channel4 =",channel)
    baudrate4 = bitrate
    print("baudrate4 =",baudrate4)
    bus = can.Bus(interface="pcan", channel=channel4, bitrate=baudrate4)
    msg_id = 0x00
    data = [0x81, node_id]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"🔄 Reset command sent to Node {node_id}. Restarting...")
    except can.CanError as e:
        print(f"❌ Failed 1: {e}")
    bus.shutdown()


def scan_nodesp(channel, bitrate,):
    channel5 = channel
    print("channe5 =",channel)
    baudrate5 = bitrate
    print("baudrate5 =",baudrate5)
    bus = can.Bus(interface="pcan", channel=channel5, bitrate=baudrate5)
    print("🔍 Scanning CANopen nodes...")

    found_nodes = []
    for node_id in range(1, 128):
        msg_id = 0x600 + node_id
        data = [0x40, 0x18, 0x10, 0x04, 0x00, 0x00, 0x00, 0x00]
        message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

        try:
            bus.send(message)
            response = bus.recv(timeout=0.5)
            if response and response.arbitration_id == (0x580 + node_id):
                print(f"✅ Active Node Found: {node_id}, Response: {list(response.data)}")
                found_nodes.append(node_id)
        except can.CanError:
            pass

    if not found_nodes:
        print("⚠️ No active nodes found!")
    print("🔍 Scan complete.")
    bus.shutdown()
def switch_to_pre_operationalrot(node_id,channel, bitrate,):
    """Send NMT command to switch the node to Pre-Operational Mode."""
    channel1 = channel
    #print("channel1 =",channel1)
    baudrate1 = bitrate
    #print("baudrate1 =",baudrate1)
     
    bus = can.Bus(interface="pcan", channel=channel1, bitrate=baudrate1)
    msg_id = 0x00
    data = [0x80, node_id]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"🔄 Node {node_id} switched to Pre-Operational Mode.")
    except can.CanError as e:
        print(f"❌ Failed4: {e}")
    bus.shutdown()
def change_node_idrot(self,current_node_id, new_node_id,channel, bitrate,):
    channel2 = channel
    #print("channel2 =",channel2)
    baudrate2 = bitrate
    #print("baudrate2 =",baudrate2)
    bus = can.Bus(interface="pcan", channel=channel2, bitrate=baudrate2)
    time.sleep(3)
    msg_id = 0x600 + current_node_id
    data = [0x2F, 0x00, 0x30, 0x00, new_node_id, 0x00, 0x00, 0x00]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        #print(f"✅ Node ID change request sent from {current_node_id} to {new_node_id}.")
        response = bus.recv(timeout=1)
        if response and response.arbitration_id == (0x700 + current_node_id):
            print(f"✅ Device acknowledged: {list(response.data)}")
        else:
            print("⚠️ No acknowledgment.")
    except can.CanError as e:
        print(f"❌ Failed 3: {e}")
    bus.shutdown()
def save_parametersrot(self,node_id,channel, bitrate,):
    channel3 = channel
    #print("channel3 =",channel3)
    baudrate3 = bitrate
    #print("baudrate3 =",baudrate3)
    bus = can.Bus(interface="pcan", channel=channel3, bitrate=baudrate3)
    msg_id = 0x600 + node_id
    data = [0x22, 0x10, 0x10, 0x01, 0x73, 0x61, 0x76, 0x65]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"💾 Save command sent to Node {node_id}.")
    except can.CanError as e:
        print(f"❌ Failed 2: {e}")
    bus.shutdown()
def switch_to_pre_operationalrotb(node_id,channel, bitrate,):
    """Send NMT command to switch the node to Pre-Operational Mode."""
    channel1 = channel
    #print("channel1 =",channel1)
    baudrate1 = bitrate
    #print("baudrate1 =",baudrate1) 
    bus = can.Bus(interface="pcan", channel=channel1, bitrate=baudrate1)
    msg_id = 0x00
    data = [0x80, node_id]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"🔄 Node {node_id} switched to Pre-Operational Mode.")
    except can.CanError as e:
        print(f"❌ Failed4: {e}")
    bus.shutdown()
def change_node_idrotb(self,current_node_id, new_node_id,channel, bitrate,):
    channel2 = channel
    #print("channel2 =",channel2)
    baudrate2 = bitrate
    #print("baudrate2 =",baudrate2)
    new_node_id = int(new_node_id)
    bus = can.Bus(interface="pcan", channel=channel2, bitrate=baudrate2)
    time.sleep(3)
    msg_id = 0x600 + current_node_id
    data = [0x2F, 0x01, 0x30, 0x00, new_node_id, 0x00, 0x00, 0x00]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"✅ Node ID change request sent from {current_node_id} to {new_node_id}.")
        response = bus.recv(timeout=1)
        if response and response.arbitration_id == (0x700 + current_node_id):
            print(f"✅ Device acknowledged: {list(response.data)}")
        else:
            print("⚠️ No acknowledgment.")
    except can.CanError as e:
        print(f"❌ Failed 3: {e}")
    bus.shutdown()
def save_parametersrotb(self,node_id,channel, bitrate,):
    channel3 = channel
    #print("channel3 =",channel3)
    baudrate3 = bitrate
    #print("baudrate3 =",baudrate3)
    bus = can.Bus(interface="pcan", channel=channel3, bitrate=baudrate3)
    msg_id = 0x600 + node_id
    data = [0x22, 0x10, 0x10, 0x01, 0x73, 0x61, 0x76, 0x65]
    message = can.Message(arbitration_id=msg_id, data=data, is_extended_id=False)

    try:
        bus.send(message)
        print(f"💾 Save command sent to Node {node_id}.")
    except can.CanError as e:
        print(f"❌ Failed 2: {e}")
    bus.shutdown()
if __name__ == "__main__":
    try:
        app = QtWidgets.QApplication(sys.argv)
        window = CANConfigApp()
        window.show()
        sys.exit(app.exec_())
        log_errors()
    except Exception:
        log_errors()
    
