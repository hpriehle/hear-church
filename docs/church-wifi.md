# Hear Church on meetinghouse Wi-Fi

For Harrison, as the building's Stake Technology Specialist (STS). Written 2026-11-22.

Two things must work:

- **(a) Internet.** The church computer reaches hearchurch.com (HTTPS and a WebSocket).
- **(b) Live.** Phones on the meetinghouse Wi-Fi talk straight to the church computer (Bonjour plus UDP port 47300).

The Church controls the firewall. An STS can do some things in Church Network Manager (CNM), and must ask for the rest. Each step below says which kind it is.

## 1. What the app talks to

From the code in `mac/` and `app/` (main branch, plus the Live code on the `chat/hc-live-*` branches).

| Host | Port | Why | When |
|---|---|---|---|
| `hearchurch.com` | TCP 443 | Sign-in, `/health` check, `/api/hc/*`, WebSocket `wss://hearchurch.com/ws/engine` (captions and audio to listeners) | Every meeting |
| `hearchurch.com/appcast.xml` (Intel: `/appcast-intel.xml`) | TCP 443 | Sparkle update check | In the background |
| `hearchurch.com/download/…` | TCP 443 | App updates and Wolof libraries (served from Cloudflare R2, but under hearchurch.com) | Updates, first Wolof use |
| `api.anthropic.com` | TCP 443 | Translation (Claude) and the API key check | Every meeting |
| `claude.ai` and its download host | TCP 443 | One-time install of Claude Code, if a subscription token is used | Setup only |
| `huggingface.co` (and its file CDN) | TCP 443 | Speech models and voices | First download of a language |
| `ebible.org` | TCP 443 | Scripture text | First download of a language |
| Local: `127.0.0.1` | any | The app talks to its own engine | Always (not affected by Wi-Fi) |
| Local Wi-Fi: Bonjour `_hearchurch-live._udp` (UDP 5353, multicast) | UDP 5353 | Phones find the Mac | Live |
| Local Wi-Fi: Mac UDP 47300, both ways | UDP 47300 | Phones send SUBSCRIBE, the Mac sends audio back | Live |

`hub.clntacq.com` is **not** used by the Mac app. It only appears in the download page's stats script (`download/src/collect.js`). `live.clntacq.com` only appears in tests and old notes.

So on the day of a meeting, the firewall must allow **hearchurch.com** and **api.anthropic.com**. The other hosts matter only for setup, downloads and updates.

## 2. Why it failed on 2026-11-22

What happened at the Harlem meetinghouse at 09:00:

- The Mac was on the church Wi-Fi.
- The app said it couldn't reach Hear Church and to check the internet connection.
- The hub logged **zero** requests, so the traffic was stopped inside the building.
- On the iPhone hotspot it worked.

The message comes from the app's preflight check: `GET https://hearchurch.com/health` with an 8-second timeout (`mac/HearChurch/Sources/HubClient.swift`). Any failure (timeout, DNS, TLS) shows as "Can't reach hearchurch.com". The app does not say which failure it was.

Two causes fit. The on-site test in section 5 tells them apart.

1. **The Liahona splash page was not accepted that day.** After the password, Liahona shows a terms-of-use page. The acceptance lasts about 24 hours. Until it is accepted, the firewall holds back the device's traffic, so nothing reaches us. Apps can't click through this page; only a browser can. ([WAP overview](https://www.churchofjesuschrist.org/tools/help/wap-overview?lang=eng), [CNM Group Policies](https://tech.churchofjesuschrist.org/wiki/CNM_Group_Policies), and a forum post on the [24-hour expiry](https://tech.churchofjesuschrist.org/forum/viewtopic.php?t=39634).)
2. **The content filter blocked hearchurch.com.** The meetinghouse firewall is a Meraki MX, and its content filtering is powered by Cisco Talos ([Church firewall overview](https://www.churchofjesuschrist.org/tools/help/firewall-overview?lang=eng), [Meraki: Content Filtering powered by Talos](https://documentation.meraki.com/MX/Content_Filtering_and_Threat_Protection/Content_Filtering_Powered_By_Cisco_Talos)). hearchurch.com was registered in 2026. Talos has a threat category, **Newly Seen Domains**, for domains that are "recently registered, or not yet seen via telemetry" ([Talos categories](https://talosintelligence.com/categories), [Talos content categories](https://support.talosintelligence.com/docs/content-categories/)). Cisco warns that blocking this category also blocks legitimate sites ([Umbrella NSD note](https://www.cisco.com/c/en/us/support/docs/security/umbrella/224735-enable-newly-seen-domains-security.html)). For HTTPS, the MX reads only the domain name (TLS SNI), and a blocked request "times out and results in an error". That matches what we saw: no block page, nothing at the hub.

We don't know the Church's filter settings, so we can't say which one it was without the test.

Not the cause: QUIC (the app uses ordinary HTTPS over TCP), and Cloudflare (it worked on the hotspot).

**Live on the hotspot.** The default iPhone hotspot gave the Mac `192.0.0.2/32`. That is a one-address network, so phones could not reach the Mac. **Personal Hotspot > Maximize Compatibility** gives a normal `172.20.10.x` network, and then Live worked. Keep that as the backup plan.

## 3. Fix (a): the Mac reaches hearchurch.com

### Step A1. Stop the splash page for the Mac (you can do this as STS)

The CNM **NoSplash** group policy lets a device skip the terms page. It is meant for devices with no screen, like printers. A Mac that runs unattended on Sunday morning fits the same need. Steps from the [CNM Group Policies wiki](https://tech.churchofjesuschrist.org/wiki/CNM_Group_Policies) (edited 2026-11-19):

1. On the Mac, give it a fixed Wi-Fi address for Liahona. Otherwise the MAC address can change and NoSplash stops matching.
   1. **System Settings > Wi-Fi**.
   2. Click **Details…** next to **Liahona**.
   3. Set **Private Wi-Fi address** to **Off** (or **Fixed**). Click **OK**.
   4. Write down the **Wi-Fi address** shown there (like `a1:b2:c3:d4:e5:f6`).
2. Join **Liahona** and accept the terms page once in Safari. Leave the Mac connected for a few minutes, so CNM can see it.
3. On any computer, open **https://cnm.churchofjesuschrist.org** and sign in with your Church Account.
4. Find the meetinghouse. Click the **firewall serial number**.
5. On **Firewall Details**, open **Menu** (far right) and choose **Group Policy**.
6. Under **Find a Device**, set **Policy Group** to **NoSplash**.
7. Pick the Mac in the **MAC Address** list, or type the address from step 1.4.
8. In **Name**, type something clear, like `Hear Church Mac - chapel`. Click **Save**.
9. Wait several minutes before testing. The wiki says the bypass takes a few minutes to start.

Notes:

- Forum posts mention a limit of about 9 to 15 NoSplash devices per building ([forum](https://tech.churchofjesuschrist.org/forum/viewtopic.php?t=37988)). Not official.
- Do **not** use the **IoT** group policy or the IOTConnect SSID. Devices there "will not be directly reachable from within the meetinghouse", which breaks Live, and that SSID is capped at 5 Mbps ([IOTConnect SSID wiki](https://tech.churchofjesuschrist.org/wiki/IOTConnect_SSID)).
- NoSplash does not change filtering. If the test in section 5 shows hearchurch.com itself is blocked, go on to A2.

### Step A2. Get hearchurch.com unblocked (the Church and Cisco decide)

You **cannot** change the firewall yourself. The Church's technology standards say: "Viewing or making changes to the configuration of a meetinghouse firewall is prohibited" (6.4.2). Filtering standards belong to the Information Security and Compliance Department. "Under the direction of the stake president, TSs can choose from approved filtering guidelines for each building" (6.3.2). Bypassing the firewall needs the stake president's permission and is only for brief, urgent uses (6.4.4). ([Technology Standards](https://www.churchofjesuschrist.org/tools/help/technology-standards?lang=eng), published 2026-08-24.)

So there are two paths. Do both.

**Path 1: fix the category at Cisco Talos.** As the owner of hearchurch.com, you can do this yourself.

1. Open **https://talosintelligence.com/reputation_center** and search `hearchurch.com`.
2. Note the **Content Category** and any **Threat Category**.
3. If it says **Newly Seen Domains**, uncategorized, or anything wrong, file a dispute through the [Talos support site](https://talosintelligence.com/reputation_center/support). You need a free Cisco account. Choose platform **Meraki MX**. Ask for content category **Religion** (or **Computers and Internet**). Meraki notes that threat-category disputes may need Meraki Support, which only the Church can contact for its firewalls ([Meraki doc](https://documentation.meraki.com/MX/Content_Filtering_and_Threat_Protection/Content_Filtering_Powered_By_Cisco_Talos)).
4. Track it at **https://talosintelligence.com/tickets**.
5. "Newly seen" also wears off by itself as the domain gets more traffic.

**Path 2: ask the Church.** The official order for help is ([Meetinghouse Technology help](https://www.churchofjesuschrist.org/help/support/meetinghouse-technology)):

1. Look for an answer at **mhtech.ChurchofJesusChrist.org**.
2. Send feedback to the Meetinghouse Technology team: **mht@ChurchofJesusChrist.org** (the address the Technology Standards and the [CNM overview](https://www.churchofjesuschrist.org/tools/help/cnm-overview?lang=eng) give).
3. Then ask the Global Service Department (GSD).

On the forum, a Church moderator said filtering requests can go through the **feedback tool in Technology Manager**, and "we will review each request". A newer post says STS site suggestions seem to have been removed ([forum thread](https://techa.churchofjesuschrist.org/forum/viewtopic.php?p=238527)). Try the feedback tool first, then email.

What to write (keep it short):

> Building: Harlem meetinghouse (New York), firewall serial `<from CNM>`.
> Please allow `hearchurch.com` and `api.anthropic.com` on the meetinghouse filter.
> Hear Church gives live captions and translated audio of sacrament meeting for members who don't speak English or are hard of hearing. The church computer must reach these domains over HTTPS (TCP 443).
> hearchurch.com was registered in 2026 and may be blocked as a newly seen domain.
> Approved by: `<stake president's name>`.

Get the stake president's OK before you send it. The standards put filtering choices under his direction.

### Step A3. Wired is an option, not a fix

The standards say wired ports should be in the bishop's and clerks' offices ([Technology Standards](https://www.churchofjesuschrist.org/tools/help/technology-standards?lang=eng), 6.5.2). Wired traffic still goes through the same firewall and filter. Plugging in may skip the splash page, but it won't unblock a filtered domain. For Live, see B3.

## 4. Fix (b): Live, phones reach the Mac

What Live needs:

- The Mac advertises `_hearchurch-live._udp` over Bonjour (multicast UDP 5353).
- Each phone sends small UDP packets to the Mac on port **47300**, and the Mac sends audio back to the phone.
- If Bonjour is blocked but unicast works, a phone can still connect by typing the Mac's address under **Details** in the app.

What can block it:

- **Client isolation.** On Meraki access points, "Layer 2 LAN isolation" means clients "will only be able to communicate with the default gateway". It blocks unicast and broadcast between wireless clients, so UDP 47300 too. On NAT-mode SSIDs it is always on. For a wired device on the same VLAN, the wired side can start traffic, but "return traffic from the client will be blocked" ([Meraki: Wireless Client Isolation](https://documentation.meraki.com/MR/Firewall_and_Traffic_Shaping/Wireless_Client_Isolation)).
- **Bonjour across isolation.** Meraki can forward Bonjour only between VLANs, from a fixed list or "All services". On the same VLAN with isolation on, "the service will not be discoverable" ([Meraki: Bonjour Forwarding](https://documentation.meraki.com/MR/Client_Addressing_and_Bridging/Bonjour_Forwarding)).

We don't know for sure whether Liahona isolates clients. A forum reply from April 2026 says "There has not been a change in the network zone isolation." It blamed a Cisco firmware bug affecting multicast, now patched, and said AirPlay TVs "should be on Liahona". That means phone-to-device traffic on Liahona is supposed to work. In July 2026, posters said the Church is replacing Meraki access points and switches with Cisco ones ([forum thread](https://tech.churchofjesuschrist.org/forum/viewtopic.php?p=262853)). So test it in each building (section 5).

### Step B1. Put the Mac and the phones on Liahona

1. The Mac joins **Liahona**, not IOTConnect and not a guest network.
2. Phones join **Liahona** and accept the terms page.
3. On the Mac, when macOS asks whether Hear Church may find devices on the local network, click **Allow**. If you missed it: **System Settings > Privacy & Security > Local Network**, turn on **Hear Church**.
4. If the macOS firewall is on (**System Settings > Network > Firewall**), click **Options…** and allow incoming connections for **Hear Church**.

### Step B2. If phones can't reach the Mac on Liahona

You can't turn isolation off; that is the Church's access point configuration. Ask through the same path as A2 (mht@ChurchofJesusChrist.org, then GSD). Ask for one of these, in this order:

1. Confirm whether Liahona has client isolation on in this building. If it does, ask whether it can allow UDP port 47300 and Bonjour (mDNS) between clients.
2. Or add the Mac's MAC address to the access points' isolation exception list. Meraki allows up to 16 addresses (MR 31.1.6 or later). Meraki also says to add the mDNS multicast addresses `01:00:5E:00:00:FB` and `33:33:00:00:00:FB` for same-subnet Bonjour ([Meraki: Wireless Client Isolation](https://documentation.meraki.com/MR/Firewall_and_Traffic_Shaping/Wireless_Client_Isolation)).

The **FAC (facilities) zone** is for building controllers that must be reached locally. The facilities manager group sets it up, not the STS ([networking overview](https://www.churchofjesuschrist.org/tools/help/networking-overview?lang=eng)). Phones aren't on it, so it doesn't help Live.

### Step B3. Wired Mac

If the Mac is on a wired office port and the phones are on Liahona, it works only if the two are routed to each other and Liahona doesn't isolate. Run the same test (section 5) with the Mac plugged in.

### Step B4. Backup that always works

iPhone **Personal Hotspot** with **Maximize Compatibility** on. The Mac and the listeners' phones join the hotspot. It costs mobile data and a hotspot takes only a few devices, so it is fine for a test or a small group.

## 5. On-site checklist (5 minutes)

Bring the Mac and an iPhone with the Hear Live app. Write down each result.

**Internet (2 minutes)**

1. Mac on **Liahona**. Open Safari and go to `http://captive.apple.com`.
   - The terms page appears: the splash page wasn't accepted. Accept it and go on. If NoSplash is set up, this means it isn't matching yet (check the Private Wi-Fi address setting, wait a few minutes).
   - The page says **Success**: no splash page in the way.
2. In Safari, open `https://www.churchofjesuschrist.org`. It should load.
3. In Safari, open `https://hearchurch.com/health`.
   - Shows `{"ok":true,...}`: hearchurch.com is reachable. Go to step 5.
   - Spins, then fails (while step 2 worked): **the filter blocks hearchurch.com**. Do A2.
4. In Terminal, to confirm:
   ```
   curl -sS -m 10 -o /dev/null -w '%{http_code}\n' https://hearchurch.com/health
   curl -sS -m 10 -o /dev/null -w '%{http_code}\n' https://api.anthropic.com/v1/models
   ```
   `200` (or `401` for Anthropic without a key) means it got through. A timeout or `000` means it's blocked.
5. Open Hear Church. The checks before **Start meeting** should say **Internet connected**.

**Live (3 minutes)**

6. Mac: in Hear Church, turn **Live English** on.
7. Mac Terminal: `ipconfig getifaddr en0`. Write down the address (like `10.x.x.x`).
8. iPhone on **Liahona**. In **Settings > Wi-Fi > (i)** next to Liahona, note its IP address. Check it is on the same network as the Mac (the first numbers match).
9. Mac Terminal: `ping -c 3 <iPhone address>`.
   - Replies: wireless clients can reach each other.
   - No replies: probably client isolation (B2). Try once more with the iPhone screen on, since phones sleep.
10. Mac Terminal: `dns-sd -B _hearchurch-live._udp` (stop with Ctrl-C). The Mac should list itself. That only shows the Mac is advertising.
11. iPhone: open Hear Live.
    - It finds the Mac and plays: Live works.
    - It doesn't find the Mac: Bonjour is blocked. Under **Details**, type the Mac's address from step 7. If that plays, Bonjour is blocked but unicast works, so typing the address is enough.
    - Neither works: isolation. Ask as in B2, and use B4 for now.

## 6. What the app could do to cope (proposals only)

None of this is built. Each item is small.

1. **Say which failure it is.** When `/health` fails, also fetch `http://captive.apple.com/hotspot-detect.html` and a big, old site (`https://www.apple.com`). Then show one of:
   - "This Wi-Fi wants you to accept its terms. Open Safari, accept, then try again." (captive page seen)
   - "This network blocks hearchurch.com. The building's technology specialist can ask for it to be allowed. Use a phone hotspot for now." (other sites work, ours doesn't)
   - "No internet." (nothing works)
   Also keep the error kind (timeout, DNS, TLS) in the log, so the next report says which one it was.
2. **Open the terms page for the operator.** If a captive page is seen, offer a button that opens Safari to `http://captive.apple.com`, so macOS shows the terms page.
3. **Show the Mac's Wi-Fi MAC address** and whether Private Wi-Fi address is on, in Settings or Help, with a link to this doc. That is what the STS types into NoSplash.
4. **Warn on a one-address hotspot.** If the Mac's address is `192.0.0.x` with no usable network, tell the operator to turn on Maximize Compatibility on the iPhone for Live.
5. **Tell the phone why Live didn't connect.** If Hear Live finds the Mac over Bonjour but gets no HELLO within 3 seconds, say "This Wi-Fi blocks phones from reaching the church computer." If it finds nothing, suggest typing the address under Details.
6. **Fallback host (bigger change, think first).** A second hub address on an older, already-categorized domain would get past a Newly Seen Domains block. Costs: a second domain and certificate to run, and it may hide the real problem. Fix the category at Talos first (A2, Path 1).

## Sources

Church (official):

- Technology Standards (2026-08-24): https://www.churchofjesuschrist.org/tools/help/technology-standards?lang=eng
- Meetinghouse Technology help: https://www.churchofjesuschrist.org/help/support/meetinghouse-technology
- Firewall overview: https://www.churchofjesuschrist.org/tools/help/firewall-overview?lang=eng
- Networking overview: https://www.churchofjesuschrist.org/tools/help/networking-overview?lang=eng
- Wireless access points overview: https://www.churchofjesuschrist.org/tools/help/wap-overview?lang=eng
- CNM overview: https://www.churchofjesuschrist.org/tools/help/cnm-overview?lang=eng
- Church Network Manager: https://cnm.churchofjesuschrist.org

Church Tech wiki and forum (the wiki "may not reflect official Church information"; forum posts are members' reports):

- CNM Group Policies (NoSplash, IoT): https://tech.churchofjesuschrist.org/wiki/CNM_Group_Policies
- IOTConnect SSID: https://tech.churchofjesuschrist.org/wiki/IOTConnect_SSID
- AirPlay / isolation / Cisco AP change, 2026: https://tech.churchofjesuschrist.org/forum/viewtopic.php?p=262853
- Getting a site unblocked: https://techa.churchofjesuschrist.org/forum/viewtopic.php?p=238527
- NoSplash for Macs: https://tech.churchofjesuschrist.org/forum/viewtopic.php?t=37988
- Splash page expiry: https://tech.churchofjesuschrist.org/forum/viewtopic.php?t=39634

Cisco Meraki and Talos:

- Wireless Client Isolation: https://documentation.meraki.com/MR/Firewall_and_Traffic_Shaping/Wireless_Client_Isolation
- Bonjour Forwarding: https://documentation.meraki.com/MR/Client_Addressing_and_Bridging/Bonjour_Forwarding
- Content Filtering powered by Talos: https://documentation.meraki.com/MX/Content_Filtering_and_Threat_Protection/Content_Filtering_Powered_By_Cisco_Talos
- Talos categories: https://talosintelligence.com/categories and https://support.talosintelligence.com/docs/content-categories/
- Talos reputation lookup: https://talosintelligence.com/reputation_center
- Umbrella, Newly Seen Domains: https://www.cisco.com/c/en/us/support/docs/security/umbrella/224735-enable-newly-seen-domains-security.html
