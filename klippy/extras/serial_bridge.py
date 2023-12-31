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

DGUS_KEY_BED_LEVEL = 0X1044

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

        self.printer = config.get_printer()
        self.name = config.get_name()
        self.reactor = self.printer.get_reactor()
        self.printer.load_object(config, 'heaters')
        self.log(config.getlist('heater', ("extruder", "heater_bed")))
        self.heater_names = config.getlist("heater", ("extruder", "heater_bed"))
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
        self._update_interval = .5
        self._update_timer = self.reactor.register_timer(self._send_update)
        self.gcode = self.printer.lookup_object('gcode')

        self.input_buffer = ""
        
    def update_leds(self, led_state, print_time):
        self.log(f"Update leds {led_state} {print_time}")

    def _send_update(self, eventtime):
        self.log("Send update timer")
        for heater in self.heaters:            
            current_temp, target_temp = heater.get_temp(eventtime)
            self.log("Temp: " + str(current_temp))
            if(heater.name == 'heater_bed'):
                self.send_text("main.bedtemp.txt=\"" f'{current_temp:.2f}' + "\"")
            else:
                self.send_text("main.nozzletemp.txt=\"" f'{current_temp:.2f}' + "\"")
        
        if self._is_led_on(eventtime):
            self.send_text("status_led2=1")
        else:
            self.send_text("status_led2=0")

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

        if(message.command == DGUS_CMD_READVAR):
            if(message.command_address == DGUS_KEY_BED_LEVEL):
                if(message.command_data[0] == 0x8):
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
        

    def log(self, msg, *args, **kwargs):
        logging.info("SERIAL BRIDGE: " + str(msg))

def load_config(config):
    return PrinterSerialBridge(config)
