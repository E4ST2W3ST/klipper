# Support for "serial bridge"
#
# Copyright (C) 2019-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

QUERY_TIME = .02

POSTFIX = b'\xff\xff\xff'

SERIAL_STATE_HEADER_NONE = 0
SERIAL_STATE_HEADER_ONE = 1
SERIAL_STATE_HEADER_TWO = 2
SERIAL_STATE_HEADER_MESSAGE = 3

SERIAL_HEADER_BYTE_1 = 0x5a
SERIAL_HEADER_BYTE_2 = 0xa5

DGUS_CMD_WRITEVAR = 0x82
DGUS_CMD_READVAR = 0x83

DGUS_KEY_MAIN_PAGE = 0x1002
DGUS_KEY_ADJUSTMENT = 0x1004
DGUS_KEY_TEMP_SCREEN = 0x1030
DGUS_KEY_COOL_SCREEN = 0x1032
DGUS_KEY_HEATER0_TEMP_ENTER = 0x1034
DGUS_KEY_HOTBED_TEMP_ENTER = 0x103A
DGUS_KEY_SETTING_SCREEN = 0x103E
DGUS_KEY_BED_LEVEL = 0x1044
DGUS_KEY_AXIS_PAGE_SELECT = 0x1046
DGUS_KEY_XAXIS_MOVE_KEY = 0x1048
DGUS_KEY_YAXIS_MOVE_KEY = 0x104A
DGUS_KEY_ZAXIS_MOVE_KEY = 0x104C

class Message:
    DATAGRAM_STATE_NONE = 0
    DATAGRAM_STATE_COMMAND = 1
    DATAGRAM_STATE_ADDRESS_1 = 2
    DATAGRAM_STATE_ADDRESS_2 = 3
    DATAGRAM_STATE_DATA_LENGTH = 4
    DATAGRAM_STATE_DATA = 5

    def __init__(self):
        self.command = None
        self.payload = []
        self.length = None
        self.command_data_length = None
        self.command_data = None
        self.command_address = None

    def process_datagram(self):
        self.command = self.payload[0]
        self.command_address = ((self.payload[1] & 0xff) << 8) | (self.payload[2] & 0xff)
        self.command_data_length = self.payload[3]

        self.command_data = []
        it = iter(self.payload[4:])
        for byte in it:
            self.command_data.append(((byte & 0xff) << 8) | (next(it) & 0xff))

    def __str__(self):      

        payload_str = ' '.join([f'0x{byte:02x}' for byte in self.payload])
        return f'payload: { payload_str }, length: {self.length}, command: 0x{self.command:02x}, command_address: 0x{self.command_address:04x} command_data_length: {self.command_data_length}, command_data: {self.command_data}'

class PrinterSerialBridge:
    def __init__(self, config):
        self.log("Init")
        self._serial_state = None
        self._serial_state = SERIAL_STATE_HEADER_NONE
        self._axis_unit = 1
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.reactor = self.printer.get_reactor()
        self.printer.load_object(config, 'heaters')
        self.log(config.getlist('heater', ("extruder", "heater_bed")))
        self.heater_names = config.getlist("heater", ("extruder", "heater_bed"))
        self._gcode_event_script = ''
        self.heaters = []

        self.leds = []

        self.printer.register_event_handler("klippy:ready", self.handle_ready)

        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command("SERIAL_BRIDGE_SEND", self.cmd_SERIAL_BRIDGE_SEND)

        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command("SERIAL_BRIDGE_STATS", self.cmd_SERIAL_BRIDGE_STATS)

        self.gcode.register_output_handler(self._output_callback)

        ppins = self.printer.lookup_object("pins")
        pin_params = ppins.lookup_pin(config.get("pin"))
        self.mcu = pin_params['chip']
        self.oid = self.mcu.create_oid()
        self.mcu.register_config_callback(self.build_config)
        self._update_interval = 2
        self._update_timer = self.reactor.register_timer(self._send_update)
        self.gcode = self.printer.lookup_object('gcode')

        self.input_buffer = ""
        
    def update_leds(self, led_state, print_time):
        self.log(f"Update leds {led_state} {print_time}")

    def _send_update(self, eventtime):
        self.log("Send update timer")
        for heater in self.heaters:            
            current_temp, target_temp = heater.get_temp(eventtime)            
            if(heater.name == 'heater_bed'):
                self.send_text("main.bedtemp.txt=\"" f'{current_temp:.0f} / {target_temp:.0f}' + "\"")
            else:
                self.send_text("main.nozzletemp.txt=\"" f'{current_temp:.0f} / {target_temp:.0f}' + "\"")
        
        if self._is_led_on(eventtime):
            self.send_text("status_led2=1")
        else:
            self.send_text("status_led2=0")

        g_status = self.printer.lookup_object("gcode_move").get_status()
        self.log(f"status: { g_status}")

        self.send_text(f"main.xvalue.val={(g_status['position'].x * 100):.0f}")
        self.send_text(f"main.yvalue.val={(g_status['position'].y * 100):.0f}")
        self.send_text(f"main.zvalue.val={(g_status['position'].z * 1000):.0f}")

        self.send_text(f"printpause.zvalue.val={(g_status['position'].z * 10):.0f}")
        
        heater_fans = self.printer.lookup_objects('heater_fan')
        #self.log(f'Fans: {fans}')

        for (name, fan) in heater_fans:
            pass
            #self.log(f'Fan status: {fan.get_status(eventtime)}')
            #self.send_text(f"printpaause.fanspeed.txt={fan.get_status(eventtime)['speed'] * 100}")
        #fan = self.printer.lookup_object("heater_fan")
        #self.log(f"Heater fan: {fan.get_status()}")
        #

        fan = self.printer.lookup_object("fan")
        self.send_text(f"printpause.fanspeed.txt=\"{(fan.get_status(eventtime)['speed'] * 100):.0f}%\"")

        #self.send

        return eventtime + self._update_interval

    def _is_led_on(self, eventtime):
        for led in self.leds:
            status = led.get_status(eventtime)
            self.log(f'{status}')
            white = status["color_data"][0][3]
            
            if(white > 0):
                return True
            else:
                return False

    def _output_callback(self, msg):
        self.log("GCODE OUTPUT: " + msg)
        self.send_text(msg)

    def cmd_SERIAL_BRIDGE_SEND(self, gcmd):
        self.log("SERIAL_BRIDGE_SEND: " + gcmd.get("TEXT"))
        self.log("SERIAL_BRIDGE_SEND: " + bytes(gcmd.get("TEXT"), 'utf-8').hex())
        self.send_text(gcmd.get("TEXT"))

    def cmd_SERIAL_BRIDGE_STATS(self, gcmd):
        self.serial_bridge_stats_cmd.send()
        self.log("SERIAL_BRIDGE_STATS" + gcmd.get("TEXT"))

    def chunkstring(self, string, length):
        return (string[0+i:length+i] for i in range(0, len(string), length))

    def send_text(self, text):
        delimiterpos = text.find("//")
        if delimiterpos >= 0:
            striped_text = text[:delimiterpos]
        else:
            striped_text = text
        if (len(striped_text) == 0):
            print("SERIAL_BRIDGE_SEND IGNORE: " + text)
            return

        chunks = self.chunkstring(striped_text + "\n", 40)
        self.send_serial(bytes(text, encoding='utf-8'))
        #for chunk in chunks:
        #    self.serial_bridge_send_cmd.send([self.oid, bytes(chunk, encoding='utf-8')])
        #    print("SERIAL_BRIDGE_SEND: " + chunk)

    def send_serial(self, msg):
        self.serial_bridge_send_cmd.send([self.oid, msg + POSTFIX])

    def build_config(self):
        rest_ticks = self.mcu.seconds_to_clock(QUERY_TIME)
        clock = self.mcu.get_query_slot(self.oid)
        self.mcu.add_config_cmd("command_config_serial_bridge oid=%d clock=%d rest_ticks=%d" % (self.oid, clock, rest_ticks))

        cmd_queue = self.mcu.alloc_command_queue()

        self.mcu.register_response(self._handle_serial_bridge_response, "serial_bridge_response")
        self.serial_bridge_send_cmd = self.mcu.lookup_command(
            "serial_bridge_send oid=%c text=%*s",
            cq=cmd_queue)
        

    def _handle_serial_bridge_response(self, params):
        data = params["text"]
        byte_debug = ' '.join(['0x{:02x}'.format(byte) for byte in data])
        self.log("Response: " + byte_debug)

        messages = []
        message = Message()
        
        for byte in data:
            #self.log(f"Process data: state {self._serial_state} {message}")
            if self._serial_state == SERIAL_STATE_HEADER_NONE:
                if byte == SERIAL_HEADER_BYTE_1:
                    self._serial_state = SERIAL_STATE_HEADER_ONE
                else:
                    self._serial_state = SERIAL_STATE_HEADER_NONE
            elif self._serial_state == SERIAL_STATE_HEADER_ONE:
                if byte == SERIAL_HEADER_BYTE_2:
                    self._serial_state = SERIAL_STATE_HEADER_TWO
                else:
                    self._serial_state = SERIAL_STATE_HEADER_NONE
            elif self._serial_state == SERIAL_STATE_HEADER_TWO:
                self._serial_state = SERIAL_STATE_HEADER_MESSAGE
                message.payload = []                
                message.length = byte
            elif self._serial_state == SERIAL_STATE_HEADER_MESSAGE:                
                message.payload.append(byte)

                if(len(message.payload) == message.length):
                    messages.append(message)
                    self._serial_state = SERIAL_STATE_HEADER_NONE

        self._serial_state = SERIAL_STATE_HEADER_NONE
        
        #self.log(f"Messages {str(messages)}")

        for message in messages:
            message.process_datagram()
            self.process_message(message)

        #self.input_buffer += params["text"].hex()
        #self._process_input_buffer()

    def process_message(self, message):
        self.log("Process message: " + str(message))

        move = self.printer.lookup_object("gcode_move")
        extrusion_factor = move.extrude_factor

        if(message.command == DGUS_CMD_READVAR):
            if(message.command_address == DGUS_KEY_BED_LEVEL):
                if(message.command_data[0] == 0x8): #light toggle
                    self.log("Requested light toggle")
                    pled = self.printer.lookup_object("led")
                    for n in pled.led_helpers.keys():
                        status = pled.led_helpers[n].get_status(None)
                        self.log(f'{status}')
                        white = status["color_data"][0][3]
                        
                        if(white > 0):
                            self.gcode.run_script(f"SET_LED LED={n} WHITE=0")
                        else:
                            self.gcode.run_script(f"SET_LED LED={n} WHITE=1")
                if(message.command_data[0] == 0x0a):
                    pass
            if message.command_address == DGUS_KEY_TEMP_SCREEN:
                if message.command_data[0] == 0x5:
                    self._axis_unit = 0.1
                if message.command_data[0] == 0x6:
                    self._axis_unit = 1.0
                if message.command_data[0] == 0x7:
                    self._axis_unit = 10.0
            if message.command_address == DGUS_KEY_COOL_SCREEN:
                if message.command_data[0] == 1:
                    self.run_delayed_gcode("M104 S0")
                if message.command_data[0] == 2:
                    self.run_delayed_gcode("M140 S0")                    
            if message.command_address == DGUS_KEY_AXIS_PAGE_SELECT:
                if message.command_data[0] == 1:
                    self._axis_unit = 0.1
                elif message.command_data[0] == 2:
                    self._axis_unit = 1.0
                elif message.command_data[0] == 3:
                    self._axis_unit = 10
                elif message.command_data[0] == 4:
                    self.run_delayed_gcode("G28")
                elif message.command_data[0] == 5:
                    self.run_delayed_gcode("G28 X")
                elif message.command_data[0] == 6:
                    self.run_delayed_gcode("G28 Y")
                elif message.command_data[0] == 7:                  
                    self.run_delayed_gcode("G28 Z")
            if message.command_address == DGUS_KEY_ZAXIS_MOVE_KEY:
                current_z = move.get_status()["gcode_position"].z
                if message.command_data[0] == 0x01:
                    if move.absolute_coord:
                        self.gcode.run_script(F"G0 Z{(current_z + self._axis_unit)}")
                    else:
                        self.gcode.run_script(F"G0 Z+{self._axis_unit}")
                else:
                    if move.absolute_coord:
                        self.gcode.run_script(F"G0 Z{(current_z - self._axis_unit)}")
                    else:
                        self.gcode.run_script(F"G0 Z-{self._axis_unit}")
            if message.command_address == DGUS_KEY_YAXIS_MOVE_KEY:
                current_y = move.get_status()["gcode_position"].y
                if message.command_data[0] == 0x01:
                    if move.absolute_coord:
                        self.gcode.run_script(F"G0 Y{(current_y + self._axis_unit)}")
                    else:
                        self.gcode.run_script(F"G0 Y+{self._axis_unit}")
                else:
                    if move.absolute_coord:
                        self.gcode.run_script(F"G0 Y{(current_y - self._axis_unit)}")
                    else:
                        self.gcode.run_script(F"G0 Y-{self._axis_unit}")
            if message.command_address == DGUS_KEY_XAXIS_MOVE_KEY:
                current_x = move.get_status()["gcode_position"].x
                if message.command_data[0] == 0x01:
                    if move.absolute_coord:
                        self.gcode.run_script(F"G0 X{(current_x + self._axis_unit)}")
                    else:
                        self.gcode.run_script(F"G0 X+{self._axis_unit}")
                else:
                    if move.absolute_coord:
                        self.gcode.run_script(F"G0 X{(current_x - self._axis_unit)}")
                    else:
                        self.gcode.run_script(F"G0 X-{self._axis_unit}")
            if message.command_address == DGUS_KEY_HEATER0_TEMP_ENTER:
                temp = ((message.command_data[0] & 0xff00) >> 8) | ((message.command_data[0] & 0x00ff) << 8)
                self.run_delayed_gcode(f"M104 S{temp}")
                self.send_text("pretemp.nozzletemp.txt=\" {} / {}\"")
            if message.command_address == DGUS_KEY_HOTBED_TEMP_ENTER:
                temp = ((message.command_data[0] & 0xff00) >> 8) | ((message.command_data[0] & 0x00ff) << 8)
                self.run_delayed_gcode(f"M140 S{temp}")
                self.send_text("pretemp.bedtemp.txt=\" {} / {}\"")
            if(message.command_address == DGUS_KEY_MAIN_PAGE):
                if(message.command_data[0] == 0x1):
                    self.send_text("page printpause")          
            if message.command_address == DGUS_KEY_ADJUSTMENT:
                if message.command_data[0] == 0x02:
                    self.send_text("page printpause")
            if message.command_address == DGUS_KEY_SETTING_SCREEN:
                if message.command_data[0] == 0x1:
                    self.run_delayed_gcode("G28\ng1 f200 Z0.05")                    
                    self.send_text("page leveldata_36")
                    self.send_text("leveling_36.tm0.en=0")
                    

    def run_delayed_gcode(self, gcode):
        self._gcode_event_script = gcode
        self.reactor.register_timer(self.gcode_command_timer, self.reactor.monotonic())
        
    def gcode_command_timer(self, eventtime):
        self.gcode.run_script(self._gcode_event_script)
        self._gcode_event_script = ''
        return self.reactor.NEVER
        
    def _process_input_buffer(self):
        delimiterpos = self.input_buffer.rfind("\n")
        while delimiterpos > 0:
            commands = self.input_buffer[:delimiterpos]
            self.gcode.respond_info(commands, log=False)
            self.gcode._process_commands(commands.split("\n"))
            self.input_buffer = self.input_buffer[delimiterpos + 1:]
            delimiterpos = self.input_buffer.rfind("\n")

    def _screen_init(self, eventtime):
        self.log("Screen init")
        self.send_text("page boot") 
        self.send_text("com_star") 
        self.send_text("main.va0.val=1") 
        self.send_text("page main") 
        self.reactor.update_timer(self._update_timer, eventtime + self._update_interval)
        return self.reactor.NEVER

    def handle_ready(self):
        self.log("ready")
        
        pheaters = self.printer.lookup_object('heaters')
        self.heaters = [pheaters.lookup_heater(n) for n in self.heater_names]

        self.reactor.register_timer(self._screen_init, self.reactor.monotonic() + 2)        

        pled = self.printer.lookup_object("led")
        self.leds =  [pled.led_helpers.get(n) for n in pled.led_helpers.keys() ]


        for n in self.printer.lookup_objects():
            self.log(f"object: {n}" )
        

    def log(self, msg, *args, **kwargs):
        logging.info("SERIAL BRIDGE: " + str(msg))

def load_config(config):
    return PrinterSerialBridge(config)
