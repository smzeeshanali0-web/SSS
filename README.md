# AI Clone Dialer

Android SIP dialer + consent-gated AI voice clone. Aap apni (ya jis ne permission di ho) awaaz ka MP3 upload karte hain, owner consent accept karta hai, clone banta hai, aur SIP call mein samne wale ko **cloned awaaz** sunai deti hai.

```
Android app ──(mic PCM16 16k, websocket)──► ai-backend ──► voice provider (ElevenLabs / self-hosted)
     ▲                                          │ converted voice → G.711 RTP
     └────(remote audio, websocket)◄──── SIP gateway ◄──► aapka SIP provider ◄──► phone
```

Why a backend gateway: Android par SIP media ke andar clone awaaz inject karne ke liye native PJSIP chahiye hota hai. Yahan SIP + RTP backend par chalta hai (pure Python, `ai-backend/app/sip.py`), app sirf mic/speaker + UI hai.

## Kya kaam karta hai
| Feature | Status |
|---|---|
| SIP REGISTER (digest auth), outgoing call, hangup, remote hangup, DTMF, re-register | ✅ fake SIP server ke against tested |
| G.711 PCMU/PCMA RTP, mute | ✅ tested |
| MP3 upload → **consent screen** → Accept = clone / Decline = sab delete | ✅ logic tested |
| Clone ke baghair ya consent revoke hone par AI call refuse | ✅ tested |
| AI voice ON + conversion fail ⇒ samne wale ko **silence**, asli awaaz kabhi nahi | ✅ tested |
| Call history, redial, multiple SIP accounts, edit/delete, speaker, mute, keypad | ✅ code (Android compile nahi hua) |
| ElevenLabs / self-hosted provider adapters | ⚠️ code likha, real account ke saath abhi test nahi hua |
| Android app | ⚠️ is environment mein compile/run nahi hui — GitHub Actions ya Android Studio se build karein |
| VICIdial / Asterisk agent-phone mode (register + incoming call auto-answer), foreground service | ✅ nakli Asterisk ke saath tested, `docs/VICIDIAL_SETUP_UR.md` |
| Leads list (add / CSV import), Next lead, notes, VICIdial-style dispositions, Do-Not-Call block | ✅ code (Android compile nahi hua) |
| Voice record in-app (sample), multiple voices, quick voice switch, **Test my cloned voice before calling** | ✅ backend tested, app code |
| ViciDial-style agent bar: READY / PAUSED + pause codes, timers, calls-today & talk-time; INCALL / DISPO call states | ✅ code (Android compile nahi hua) |
| Script / SOP panel on call screen ({name} {first} {phone} tags, editable in Settings) | ✅ code |
| Call-backs: CALLBK disposition + time choice, due call-backs come first in *Next lead*, Leads search + filters | ✅ code |
| Pre-call voice check: app asks you to test your cloned voice before an AI-voice call (switch in Settings) | ✅ code |
| Hold, TLS/TCP, SRTP, STUN, auto/predictive dialing, call recording | ❌ nahi (jaan-boojh kar: bulk AI-voice calling aur recording ke qanooni masail) |

## Latency (important)
ElevenLabs speech-to-speech chunk-by-chunk chalta hai: **~1.5–3 second delay** rehta hai. Bilkul real-time (<0.5 s) ke liye GPU par self-hosted voice-conversion (Seed-VC/RVC) chahiye; `VOICE_PROVIDER=selfhosted` contract `ai-backend/app/providers.py` ke top mein likha hai.

## Consent / qanooni baatein
- Sirf **apni awaaz**, ya us insaan ki jis ne **permission di**. Kisi singer/celebrity ki awaaz bina ijazat clone karna allowed nahi (consent screen mein bhi likha hai, aur provider terms bhi mana karte hain).
- Backend yeh verify nahi kar sakta ke awaaz waqai us ki hai; yeh ek recorded attestation hai (naam type karna, permission note, audit log, sample SHA-256). Doosre ki awaaz ke liye permission ka saboot aap apne paas rakhen.
- Call par samne wale ko batana ke awaaz AI-generated hai — kai mulkon mein qanooni zaroorat hai. Kisi ko dhoka dene / fraud / harassment ke liye istemal na karein. Bulk/robocall ke apne qawaneen hain.
- Consent revoke/delete karne par provider se voice bhi delete hoti hai. Clone banne ke baad uploaded sample server se delete ho jata hai (`KEEP_SAMPLES=1` se rakh sakte hain).

## Backend chalana
```bash
cd ai-backend
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # API_KEY, VOICE_PROVIDER, ELEVENLABS_API_KEY bharein
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
- Server ko public IP chahiye (ya `SIP_PUBLIC_IP` set karein) aur UDP ports `10000-20000` (RTP) firewall mein khule hon. SIP provider ke saath yeh server ka IP hi dikhega.
- Production: HTTPS reverse proxy (Caddy/nginx) ke peeche chalayein; app mein `https://` URL dein.
- Docker: `docker build -t aiclone ai-backend && docker run -p 8000:8000 --env-file ai-backend/.env -v aidata:/data aiclone`
- Tests: `pip install -r requirements-dev.txt && python -m pytest -q tests`

## Android APK banana
- **GitHub par:** repo mein push karein → Actions tab → "Build Android APK" → artifact `app-debug-apk` download karein.
- **Android Studio:** `android-app/` open karein → Build APK. (JDK 17, SDK 35.)

## App use karna
1. Settings → Backend URL + API key → *Test connection*.
2. Accounts → SIP account (username, password, server, port; transport UDP).
3. Voice Studio → *Add voice* → MP3 chunein → owner ka naam → **Consent screen**: Accept ⇒ clone banta hai, Decline ⇒ sab delete.
4. Voice ready ho to *Set default*; Dialer mein *Use AI cloned voice* ON karein; number dial karein.
