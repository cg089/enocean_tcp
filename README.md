# enocean_tcp
EnOcean TCP Gateway for use in Home Assistant

I built an EnOcean to ESP8266 (or ESP32) bridge using:
https://github.com/Techserv-krY/EnOcean_ESP8266_Gateway/wiki

EnOcean Pi Module:
https://www.domadoo.fr/fr/dongle-enocean/2466-enocean-module-radio-enocean-pi-868mhz.html

Wiring:

  ESP - ENOCEAN
   TX - RX  10
   RX - TX   8
  Vcc - Vcc  1
  GND - GND  6

The Gateway relays all packages on port 9999.

Just copy enocean_typ folder to Home Assistant / config / custom_components, restart, Add Integration -> enocean_tcp


In Home Assistant, listen to enocean_tcp_frame and use a EnOcean sender:

event_type: enocean_tcp_frame
data:
packet_type: 1
data_hex: F6E08100EA2720
opt_hex: 00FFFFFFFF4F00
rorg: 246
sender_id: 8100EA27
status: 32
raw: PT=01 DATA=F6E08100EA2720 OPT=00FFFFFFFF4F00
origin: LOCAL
time_fired: "2025-09-22T23:36:09.663588+00:00"
context:
id: 01K5SWHS5ZHAP2M55AW6DG8K5G
parent_id: null
user_id: null
