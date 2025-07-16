Halo oglasi watcher, emails new listings
Enable app password on gmail (outlook is nightmare) 

In main dir where main.py is, create config.yaml 

Blast this inside 

location_url: "https://www.halooglasi.com/nekretnine/prodaja-stanova/beograd?ulica_t=###EXAMPLE###  # or any search URL
email:
  smtp_server: "smtp.gmail.com"
  smtp_port: 587
  username: "sender@gmail.com"
  password: "YourGmailAppPass"        # use a Gmail App Password
  to: "YourEmailForNotifications@any.com"



Not bad.
