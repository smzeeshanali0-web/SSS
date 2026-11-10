# VICIdial ke saath istemal (Agent phone mode)

App VICIdial me **agent ka SIP phone** ban jati hai. Agent screen par aap normal tareeqe se dial/take call karte hain, aur customer ko aap ki **cloned awaaz** sunai deti hai.

```
Aap ka mic -> App -> ai-backend -> ElevenLabs (clone awaaz) -> SIP/RTP -> VICIdial (Asterisk) -> Customer
Customer ki awaaz  <-  VICIdial  <-  ai-backend  <-  App (speaker)
```

## 1. VICIdial me phone (admin ek baar karta hai)
Admin -> Phones -> Add a new phone:
- Extension / Login: `8001` (koi bhi), Password: apna
- Protocol: **SIP**, Dialplan number: `8001`, Server IP: apka VICIdial server
- **Host: dynamic**, NAT: no (ya aap ke network ke mutabiq), qualify: yes
- Codecs: **ulaw, alaw** (`disallow=all`, `allow=ulaw`, `allow=alaw`)
- DTMF: rfc2833
Phone ko Agent screen ke "Phone Login / Phone Password" me yehi extension aur password lagta hai.

## 2. Backend server
- `ai-backend` ko kisi aise server par chalayen jis ka **public IP** ho (ya `SIP_PUBLIC_IP` set karein).
- Firewall me **UDP 10000-20000** kholen. Is me RTP bhi hai aur SIP ka socket bhi isi range me hota hai.
- VICIdial server se is server tak UDP pahunchna chahiye (VICIdial isi ko call karega).
- HTTPS ke peeche chalayen (Caddy/nginx).

## 3. App me
1. **Settings:** Backend URL, API key, ElevenLabs key -> Save.
2. **Accounts -> Add SIP account:** "VICIdial / Asterisk agent phone" ON rehne den. Sirf 3 cheezein bharein: VICIdial server IP, Phone extension, Phone password.
3. **Voice Studio -> Add voice:** apni MP3 chunein, consent screen Accept karein. Status *ready* hone par **Set default**.
4. **Dialer:** "Use AI cloned voice" ON karein -> **Start VICIdial session**. "Waiting for VICIdial call" aaye ga.
5. VICIdial agent screen par login karein (Phone Login = extension). VICIdial aap ke extension ko call karega, app khud answer karegi, status *Connected* ho jaye ga.
6. Ab VICIdial me normal dial/take call karein. Customer ko clone awaaz sunai de gi.
7. Kaam khatam: app me **Hang up (End session)**.

## Zaroori baatein
- Agar AI voice ON ho aur conversion fail ho jaye to customer ko **khamoshi** sunai deti hai, asli awaaz kabhi nahi.
- Delay ElevenLabs ke saath 1.5-3 second hota hai.
- Yeh **live insaan agent** ke liye hai (aap bol rahe hain). Isme auto/predictive dialing ya khud-bolne wala AI robot nahi hai. Clone sirf apni (ya ijazat wali) awaaz ka ho aur samne wale ko batana qanooni taur par zaroori ho sakta hai.
- Maine yeh asli VICIdial server ke saath test nahi kiya; nakli Asterisk server ke saath incoming call, audio aur hangup tests pass hain. Pehli baar fail ho to `docker logs` ya backend ka log bhej dein.
