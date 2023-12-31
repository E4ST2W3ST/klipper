scp * pi@192.168.2.50:/home/pi/klipper/klippy/extras/.
ssh pi@192.168.2.50 "sudo service klipper restart"
ssh pi@192.168.2.50 "tail -f /home/pi/printer_data/logs/klippy.log"
