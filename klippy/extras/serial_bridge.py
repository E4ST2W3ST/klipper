# Support for "serial bridge"
#
# Copyright (C) 2019-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, re

QUERY_TIME = 0.2

class PrinterSerialBridge:
    def __init__(self, config):
        self.log("Init") 
        self.callbacks = []       
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.eol = config.get('eol', default='\n')

        self.reactor = self.printer.get_reactor()
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command("SERIAL_BRIDGE_SEND", self.cmd_SERIAL_BRIDGE_SEND)

        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command("SERIAL_BRIDGE_STATS", self.cmd_SERIAL_BRIDGE_STATS)

        self.gcode.register_output_handler(self._output_callback)

        ppins = self.printer.lookup_object("pins")
        pin_params = ppins.lookup_pin(config.get("tx_pin"))
        rx_pin_params = ppins.lookup_pin(config.get("rx_pin"))
        self.mcu = pin_params['chip']
        self.oid = self.mcu.create_oid()
        self.mcu.register_config_callback(self.build_config)
        self.gcode = self.printer.lookup_object('gcode')

        self.input_buffer = ""

    def register_callback(self, callback):
        self.callbacks.append(callback)

    def _output_callback(self, msg):
        self.log("GCODE OUTPUT: " + msg)
        self.send_text(msg)

    def cmd_SERIAL_BRIDGE_SEND(self, gcmd):
        self.log("SERIAL_BRIDGE_SEND: " + gcmd.get("TEXT"))
        self.log("SERIAL_BRIDGE_SEND: " + bytes(gcmd.get("TEXT"), 'utf-8').hex())
        self.send_serial(self.perform_replacement(gcmd.get("TEXT")))

    def perform_replacement(self, input_string):
        # Find all occurrences of "\x" followed by two hexadecimal digits
        hex_matches = re.finditer(r'\\x([0-9a-fA-F]{2})', input_string)

        # Replace each matched hex sequence with its corresponding bytes
        replaced_bytes = bytearray()
        last_index = 0

        for match in hex_matches:
            hex_value = match.group(1)
            byte_value = bytes.fromhex(hex_value)
            replaced_bytes.extend(byte_value)
            last_index = match.end()

        replaced_bytes.extend(input_string[last_index:].encode('utf-8'))
        
        return replaced_bytes

    def cmd_SERIAL_BRIDGE_STATS(self, gcmd):
        self.serial_bridge_stats_cmd.send()
        self.log("SERIAL_BRIDGE_STATS" + gcmd.get("TEXT"))

    def chunkstring(self, msg, length):
        return (msg[0+i:length+i] for i in range(0, len(msg), length))

    def send_text(self, msg):    
        self.send_serial(bytes(msg, encoding='utf-8'))

    def send_serial(self, msg):
        chunks = self.chunkstring(msg + self.perform_replacement(self.eol), 40)
        for chunk in chunks:
            self.log(f"Sending chunk: {chunk.hex()}")
            self.serial_bridge_send_cmd.send([self.oid, chunk])

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

        for callback in self.callbacks:
            callback(data)

    def process_message(self, message):
        self.log("Process message: " + str(message))        
        return self.reactor.NEVER

    def handle_ready(self):
        self.log("ready")        

    def log(self, msg, *args, **kwargs):
        logging.info("SERIAL BRIDGE: " + str(msg))

def load_config_prefix(config):
    return PrinterSerialBridge(config)
