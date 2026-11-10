# GitHub par upload + APK banane ka tareeqa

## Upload (browser se)
1. Zip ko **Extract All** karein, phir `AI-Clone-Dialer` folder kholein.
2. GitHub repo → **Add file → Upload files**.
3. Folder ke **andar ki tamam files/folders** (hidden `.github` folder bhi!) drag-and-drop karein. `.github` folder zaroori hai kyunke usi se APK khud ban-ta hai. Agar browser hidden folder drag nahi karta to Git Bash se:
   ```
   git init && git add . && git commit -m "AI Clone Dialer"
   git branch -M main && git remote add origin https://github.com/YOUR_USERNAME/REPO.git && git push -u origin main
   ```
4. **Commit changes**.

## APK
Repo → **Actions** tab → "Build Android APK" (push par khud chalta hai) → complete hone par neeche **Artifacts → app-debug-apk** download karein, phone mein install karein.
(Agar build fail ho to Actions ka log mujhe bhej dein, main fix kar dunga.)

## Backend (server par)
README.md ka "Backend chalana" hissa dekhein. Bina backend ke app call nahi kar sakti.
