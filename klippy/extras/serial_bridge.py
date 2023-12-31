# Support for "serial bridge"
#
# Copyright (C) 2019-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, re

QUERY_TIME = 0.2

class PrinterSerialBridge:
    def __init__(self, config):
        self.callbacks = []       
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.eol = config.get('eol', default='\n')
        self._ready = False

        self.reactor = self.printer.get_reactor()
        self.printer.register_event_handler("klippy:ready", self.handle_ready)
        self.printer.register_event_handler("klippy:disconnect", self.handle_disconnect)

        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command("SERIAL_BRIDGE_SEND", self.cmd_SERIAL_BRIDGE_SEND)

        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command("SERIAL_BRIDGE_STATS", self.cmd_SERIAL_BRIDGE_STATS)

        ppins = self.printer.lookup_object("pins")
        pin_params = ppins.lookup_pin(config.get("tx_pin"))
        rx_pin_params = ppins.lookup_pin(config.get("rx_pin"))
        self.mcu = pin_params['chip']
        self.oid = self.mcu.create_oid()
        self.mcu.register_config_callback(self.build_config)

        self.input_buffer = ""

    def register_callback(self, callback):
        self.callbacks.append(callback)    

    def cmd_SERIAL_BRIDGE_SEND(self, gcmd):
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

    def chunkstring(self, msg, length):
        return (msg[0+i:length+i] for i in range(0, len(msg), length))

    def send_text(self, msg):    
        self.send_serial(bytes(msg, encoding='utf-8'))

    def send_serial(self, msg):
        if not self._ready:
            self.log("Can't send message in a disconnected state")
            return

        chunks = self.chunkstring(msg + self.perform_replacement(self.eol), 40)
        for chunk in chunks:
            byte_debug = ' '.join(['0x{:02x}'.format(byte) for byte in chunk])
            self.log("Sending message: " + byte_debug)
            self.serial_bridge_send_cmd.send([self.oid, chunk, 4])

    def build_config(self):
        rest_ticks = self.mcu.seconds_to_clock(QUERY_TIME)
        clock = self.mcu.get_query_slot(self.oid)
        self.mcu.add_config_cmd("command_config_serial_bridge oid=%d clock=%d rest_ticks=%d config=%d baud=%u" % (self.oid, clock, rest_ticks, 4, 115200))

        cmd_queue = self.mcu.alloc_command_queue()

        self.mcu.register_response(self._handle_serial_bridge_response, "serial_bridge_response", self.oid)
        self.serial_bridge_send_cmd = self.mcu.lookup_command(
            "serial_bridge_send oid=%c text=%*s",
            cq=cmd_queue)        

    def _handle_serial_bridge_response(self, params):
        data = params["text"]

        for callback in self.callbacks:
            callback(data)

    def handle_ready(self):
        self.log("Ready")
        self._ready = True

    def handle_disconnect(self):
        self._ready = False    

    def log(self, msg, *args, **kwargs):
        logging.info("SERIAL BRIDGE: " + str(msg))

def load_config_prefix(config):
    return PrinterSerialBridge(config)
