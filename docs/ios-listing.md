# Hear Church for iPhone: App Store Connect listing

Text to paste into App Store Connect for (1) TestFlight external beta review and (2) the App Store listing.
Facts come from the hub code (`app/hear/api.py`, `app/hear/geo.py`, `app/languages.py`), the Live notes
(`docs/church-wifi.md` on `chat/hc-church-wifi`) and the live site (hearchurch.com, read 2026-11-22).
I couldn't read the iPhone app (hc-mobile is on the Mac only). Items marked **Check in hc-mobile** depend on it.

Wording rules: the feature is "Live". No pricing words about features. Listeners never pay, so the listing says
nothing about money. No other products are named.

---

## 0. What the reviewer needs first: a demo congregation (not built)

**Today there is none.** `GET https://hearchurch.com/api/hc/search?q=demo` returns `{"results":[]}`, and the hub
has no demo or test mode. A meeting is live only while a church computer publishes it over `/ws/engine`
(`app/hear/live.py`). Reviewers open the app at any hour from Cupertino. Without a live meeting they only see
"wait for the meeting to start", and the app will likely be rejected under 2.1 (incomplete) or 4.2 (minimum functionality).

**Proposal: the smallest hub-side change.** It needs no change to the hub's routes.

1. **A demo congregation row.** Name **Hear Church Demo**, slug `hear-church-demo` (not in `RESERVED_SLUGS`),
   `email_verified = 1`, `hidden = 0`, a meeting time, no building location. With no location it never shows
   in anyone's "nearby" list (`/api/live`), so real listeners near a chapel never see it. It is found by name search.
   Create it with the normal sign-up (`POST /api/hc/signup`, `verify_email` off, so it returns a device token).
2. **`scripts/demo_loop.py` (about 80 lines, a new file).** It connects to `wss://hearchurch.com/ws/engine` with
   that device token and speaks the protocol a Mac already speaks: `hello` (same `stream` settings), then
   `session_start` with `fr`, `es`, `pt` and `en`. Then it plays a recorded meeting in a loop at real-time speed:
   caption messages as `{"type":"hub","lang":…,"msg":{…}}` and MP3 as binary frames (a language-index byte, then MP3).
   On disconnect it reconnects and resumes with `resume: <session>`. It runs as a systemd unit on the hub server
   only while review is open.
   - The recording: one short meeting (2 to 3 minutes of a talk, English speaker) run once on the Mac with recording on.
     The Mac already keeps the transcript and translations in its meeting log, and the translated voice per language.
     The caption `msg` shape must be copied from what the Mac sends today.
3. **Billing:** the demo's sessions must not count toward a trial or credits. On the billing branches, seconds are
   reported by the publisher, so the script just reports none. Check that this holds on `main` when it's built.

The no-code alternative: leave a Mac running Hear Church Demo with a looping audio file as its input during
review. It works today, but it ties up a Mac for days, and every pass is real translation.

**Harrison decides:** build the demo loop (recommended), or use a Mac during review. The listing below assumes
the demo exists as **Hear Church Demo**.

---

## 1. TestFlight: external beta review

**Beta App Description** (≤4000)

> Hear Church lets you listen to your church meeting in your own language. Find your congregation once, and when
> the meeting starts, the translation plays by itself, with captions on screen. It keeps playing with the screen
> locked. If your church has turned on Live and you're on the church Wi-Fi, you hear the room's sound straight
> from the church computer.

**What to Test** (≤4000)

> - Find your congregation, by location or by name. Try "Hear Church Demo" if yours isn't live right now.
> - When the meeting is live, does it start playing by itself in your language? Are the captions easy to read?
> - Lock the screen or switch apps. Does the voice keep playing, and do the lock screen controls work?
> - Change language during the meeting.
> - Change congregation from the menu.
> - At church with Live on: join the church Wi-Fi. Does the Live pill appear, and does the sound play?
> - Tell us about anything confusing, slow or wrong in the translation: support@hearchurch.com

**Feedback email:** `support@hearchurch.com`

**Marketing URL:** `https://hearchurch.com`
**Privacy Policy URL:** `https://hearchurch.com/privacy`

**Beta App Review contact:** first name, last name, phone and email: **Harrison fills these in** (Apple calls this
number if review has questions; it isn't shown publicly).

**Sign-in required:** No (leave unchecked). There are no accounts.

**Review notes (Beta App Review):** use the App Review notes in section 2.9; they apply as is.

---

## 2. App Store listing

### 2.1 Name and subtitle

- **Name:** `Hear Church`
- **Subtitle** (29/30): `Your church, in your language`

### 2.2 Promotional text (≤170, can be changed without a new build)

> Your meeting, live in your own language. Find your congregation once; when the meeting starts, it plays by
> itself with captions.

(128 characters)

### 2.3 Description (≤4000)

> Hear Church lets you listen to your church meeting in your own language, live on your phone.
>
> Find your congregation once. Hear Church remembers it. When the meeting starts, the translation plays by itself in
> your language. You don't need to press anything. Put in your headphones and listen.
>
> • Live translation, spoken and captioned, a few seconds behind the speaker
> • Plays in the background and from the lock screen, so you can put your phone away
> • Finds congregations near you, or search by name or town
> • Change language or congregation anytime from the menu
> • No account, no sign-in
>
> LIVE
> When your church turns on Live and you're on the church Wi-Fi, you hear the sound of the room straight from the
> church computer, right as it's spoken.
>
> LANGUAGES
> Français, Español, Português, Русский, Українська, فارسی, नेपाली, Tiếng Việt, 中文, Wolof and English.
> Each congregation chooses the languages it offers.
>
> HOW IT WORKS
> Your church runs Hear Church on a computer at the meeting. It hears the speaker, translates what is said and sends
> it to the phones listening. The sound stays in the building; only text is sent out to be translated. Translation
> is done by machine, so it can make mistakes.
>
> Hear Church works for any spoken service: sermons, talks, prayers and announcements. Music is skipped. For Latter-day
> Saint wards and branches, it knows Church terms and hymn titles.
>
> Is your church not on Hear Church yet? Show your leaders hearchurch.com.

**Check in hc-mobile:** the language list is the hub's output languages (`app/languages.py`). Drop any the phone doesn't
show. The site lists the same 11.

### 2.4 Keywords (≤100, comma-separated, no spaces)

```
translation,translate,sermon,captions,interpreter,congregation,ward,branch,meeting,worship,language
```

(99 characters. "hear", "church" and "live" are left out: words in the name are indexed already, and "live" alone is too broad.)

### 2.5 URLs

- **Support URL:** `https://hearchurch.com/` (the footer of every page has `support@hearchurch.com`.
  `/support` is a 404 today. A support page would be nicer, but Apple only needs contact info reachable from the URL.)
- **Marketing URL:** `https://hearchurch.com`
- **Privacy Policy URL:** `https://hearchurch.com/privacy`. It doesn't cover the iPhone app yet: see section 3.

### 2.6 Category

- **Primary:** Reference (where translation apps sit)
- **Secondary:** Lifestyle

### 2.7 Age rating: 4+

Answer **None / No** to every item:

| Question | Answer | Why |
|---|---|---|
| Cartoon or fantasy violence, realistic violence, prolonged graphic violence | None | |
| Sexual content or nudity, graphic sexual content | None | |
| Profanity or crude humor | None | `app/profanity.py` masks swear words, vulgar words and slurs as `****` in every caption and in the voice. |
| Alcohol, tobacco, drug use or references | None | |
| Mature or suggestive themes, horror or fear themes | None | Church meetings. Religious content isn't an age-rating item. |
| Medical or treatment information | None | |
| Gambling, simulated gambling, contests | None / No | |
| Unrestricted web access | No | No browser in the app. It only talks to hearchurch.com and the church computer. |
| User-generated content | No | One-way broadcast from a verified congregation's own computer. Listeners can't post, message or upload anything, and listeners can't see each other. |
| Messaging and chat | No | |
| Advertising | No | |
| Parental controls, age assurance | No | |
| Made for Kids | No | Not aimed at children (privacy policy, "Children and sensitive meetings"). |

Result: **4+**.

### 2.8 App Privacy (the "nutrition label")

**Answer: "Data Not Collected"**, if the hc-mobile checks below hold.

Apple counts data as collected only when it leaves the device and is kept longer than needed to answer the
request in real time. The app's traffic, according to the hub:

| What the app sends | Where | Kept? | Evidence |
|---|---|---|---|
| Location (lat/lng), only after the one-time "When In Use" prompt | `GET /api/live?lat&lng` | No. Answers that request and is gone. | `api.py` `nearby()`: "Nothing about the request is kept." `geo.py` `hide_coordinates_in_access_log()` strips the query from uvicorn's access log. The Cloudflare Worker's page log (`download/src/edge-log.js`) records path, referrer host, user-agent class and country, not the query. |
| Search text | `GET /api/hc/search?q=` | No | `search()` reads the DB and answers. Nothing is written. |
| Congregation slug | `GET /api/hc/c/<slug>`, `/c/<slug>/ws/listen`, `/c/<slug>/stream/<lang>.mp3` | No | Read-only. The listen socket's hello has only `boot`, `session`, `lang`, `last_seq`. Listeners are counted live in memory (`listeners()`), never stored. |
| Live (church Wi-Fi) | Bonjour `_hearchurch-live._udp` + UDP 47300 to the church computer on the same Wi-Fi | Never reaches our server | `docs/church-wifi.md` §4 |
| Remembered congregation and language | On the phone only | | |

What third parties see: Cloudflare carries all hearchurch.com traffic, so it sees IP addresses and the request URL,
and the privacy policy already says it "may keep short-term logs". That is the network operator, not us collecting,
and it's the same for the web finder today.

**Check in hc-mobile before submitting.** If any is "yes", the answer changes:
- Does it round the location before sending (the web finder sends 3 decimals, about 100 m: `web/find/find.js:119`)?
  Not required for "Not Collected", but the privacy policy promises "rounded to about 100 meters". Make the app match.
- No analytics, crash-reporting or logging SDK, and no diagnostic log upload like the Mac app's. (Apple's own TestFlight
  and App Store crash reports don't count.) If it uploads logs, declare **Diagnostics → Crash Data / Performance Data,
  not linked to identity, App Functionality**.
- No device ID, advertising ID or push token sent anywhere. With push notifications, declare **Identifiers → Device ID**.
- The location purpose string says what it's for. Suggested:
  `NSLocationWhenInUseUsageDescription`: "Hear Church uses your location once to list congregations near you. It isn't stored."
  `NSLocalNetworkUsageDescription`: "Hear Church looks for your church's computer on this Wi-Fi to play Live."
  `NSBonjourServices`: `_hearchurch-live._udp`.

### 2.9 App Review notes (≤4000)

> Hear Church plays a church meeting, translated live, in the listener's language. There are no accounts and no sign-in.
>
> To see a meeting: open the app, choose "Search by name", type "Hear Church Demo" and tap it. That congregation plays
> a recorded meeting in a loop around the clock for review, so it is always live. It starts playing by itself in the
> phone's language if offered, otherwise in the first one. Change language from the menu. Lock the screen: the voice
> keeps playing (background audio) and the lock screen shows the controls.
>
> Real congregations are live only during their own meetings, when the church's computer starts the translation.
>
> Location: asked once, only to list congregations within about a mile. The position answers that one request and
> isn't stored or logged. Declining it is fine: search by name works without it.
>
> Local Network and "Live": a church can turn on Live, which sends the room's sound from the church computer to phones
> on the church Wi-Fi (Bonjour and UDP on that Wi-Fi). It only works inside the building, so it can't be shown in
> review. Without the church Wi-Fi the app shows "Connect to Wifi for Live" and keeps playing the translation over
> the internet.
>
> Background audio is used only to keep playing the meeting the user is listening to.
>
> Contact: support@hearchurch.com

(Edit the first paragraph if the demo isn't built. **Check in hc-mobile:** the exact labels for "Search by name"
and the menu.)

### 2.10 Export compliance

**Answer: the app uses only exempt encryption.** It uses HTTPS/WSS through Apple's networking (URLSession,
URLSessionWebSocketTask or AVPlayer) to hearchurch.com. Live is plain UDP audio on the local Wi-Fi.
In App Store Connect: "Does your app use encryption?" **Yes** → "Does it qualify for any of the exemptions?"
**Yes**: standard encryption built into the operating system only, no proprietary or non-standard algorithms.
No documentation upload needed.

Simplest: put `ITSAppUsesNonExemptEncryption` = `NO` in the app's Info.plist, and App Store Connect stops asking on every build.
**Check in hc-mobile:** no bundled crypto library (OpenSSL, libsodium) and no custom encryption on the Live UDP link.

### 2.11 Content rights

"Does your app contain, show, or access third-party content?" **Yes**, and "Do you have all necessary rights?" **Yes**.

- Meetings are published by each congregation from its own computer. The church chooses to translate them (terms of
  service; the privacy policy asks the person running it to get their leader's approval first).
- When a speaker quotes a verse, the captions may use published scripture wording (`app/scripture.py`). The texts come
  from ebible.org under their own licenses. **Check:** every translation in use allows this (public domain or CC
  with attribution). `THIRD_PARTY_NOTICES.md` has no scripture entry yet, so add the credits there if a license asks for attribution.
- Hymn titles are titles only.

### 2.12 Screenshots

Required sizes (portrait). Apple scales 6.9" down to the smaller iPhones if only 6.9" is uploaded, but the
task asks for both:

- **6.9"**: 1320 × 2868 (iPhone 16 Pro Max / 17 Pro Max simulator)
- **6.5"**: 1284 × 2778 (iPhone 14 Plus simulator), or 1242 × 2688 (iPhone 11 Pro Max / XS Max simulator)

Take them on the demo congregation, with the phone in a language other than English for some shots. Five shots, in this order:

1. **The meeting playing:** captions mid-talk in Español, the language name visible. (The main shot.)
2. **Lock screen:** the meeting playing with the lock screen controls.
3. **Finding a congregation:** the nearby list with a live one at the top (in the simulator, use a fixed
   location near a real congregation, or the search results for "Hear Church Demo").
4. **Choosing a language:** the language list open (Français, Español, Português, …).
5. **Live:** the Live pill showing in a meeting. This needs Live on the same Wi-Fi as a church computer: take it
   on the Mac with the Mac app's Live turned on. Leave it out if that isn't practical; four is fine.

Optional short caption on each (no pricing words): "Your meeting, in your language" · "Keeps playing with the screen
locked" · "Find your congregation" · "11 languages" · "Live: the room's sound on the church Wi-Fi".

---

## 3. Privacy policy: missing paragraph for the iPhone app (proposal)

The policy (effective November 16, 2026) covers listening on hearchurch.com, the site pages and the Mac app. It
doesn't mention the iPhone app or Live. Proposed changes. **Not applied: site changes are outside this task.**

Intro sentence: "…when you listen on hearchurch.com **or in the Hear Church iPhone app**, visit our pages…"

New paragraph under **If you listen**, after "Finding a congregation":

> **The iPhone app.** The app works the same way. It asks once whether to share your location, and only uses it to
> list congregations near you. Your location goes to our server rounded to about 100 meters. It answers that one
> request and isn't stored or written to our logs. The app remembers your congregation and language on your phone.
> It has no ads and no analytics, and it doesn't ask for your name or email.
>
> **Live.** If your church turns on Live, the app looks for the church's computer on the Wi-Fi you're on and plays
> the room's sound straight from it. For that, your phone asks once for permission to use the local network. That
> sound goes from the church computer to your phone over the church's Wi-Fi. It doesn't pass through our server and
> isn't saved.

Also, under **The short version**, "The sound of the meeting stays on the Mac running it" stays true for translation,
but with Live, room sound now goes to phones on the same Wi-Fi. Suggested: "The sound of the meeting stays in the
building: only the text of what was said is sent out for translation."

(**Check in hc-mobile:** "rounded to about 100 meters" and "no analytics" must be true of the app, see 2.8.
Live room sound to phones also happens from the Mac today, so this paragraph is needed even before the iPhone app ships.)
