# Garmin Connect Developer Program: application draft

Form: https://www.garmin.com/en-US/forms/StarterGarminConnectDeveloperAccess/

Everything below is a draft to copy into the form. `[brackets]` are things only you can fill in.
Keep the answers honest: Garmin's terms make the developer responsible for everything said here.

---

## Before you submit

1. **The legal entity: a Swiss sole proprietorship (Einzelunternehmen) "Dominik Reuter – VibeGrind".**
   - In Switzerland a sole proprietorship exists as soon as you run the business; the business name must
     contain your surname, which "Dominik Reuter – VibeGrind" does.
   - Entry in the commercial register (Handelsregister) is only required from CHF 100,000 annual revenue;
     a voluntary entry gives you a UID number (CHE-…) that looks good on the application and the imprint.
   - Being self-employed also has social-insurance (AHV, with the SVA Schwyz) and tax consequences once you
     earn money. A Treuhänder can tell you what applies; this isn't tax or legal advice.
2. **Domain and email.** Register `vibegrind.app` (or similar) and a matching address like
   `hello@vibegrind.app`. A custom domain looks far more credible to Garmin than `….onrender.com`.
   `vibegrind.app` looked unregistered on 2026-10-27 (`vibegrind.com` is taken by someone else).
   The landing page and legal pages already use `hello@vibegrind.app`; change it if you pick another address.
3. **Fill the placeholders** on `/privacy`, `/terms` and `/imprint` (they're highlighted in yellow), then
   remove the "Draft" notes. Have a lawyer look at the privacy policy: it covers health data (Art. 9 GDPR).
4. **Server region: done.** `render.yaml` now creates the service in Render's **Frankfurt** region (EU).
   The privacy policy says so.
4b. **EU representative: Dominik Reuter, Kobenbachweg 5, 88090 Immenstaad, Germany** (in the privacy policy).
   Ask your lawyer to confirm this works: naming yourself as your own EU representative is unusual, and if
   you live in Germany, the business may count as established in Germany rather than in Switzerland.
5. **Make the promises true before onboarding other users** (the current app only connects your own key):
   - Explicit consent screen when someone connects Garmin (health data).
   - "Disconnect Garmin and delete my Garmin data" button.
   - "Delete my account" (today: on request by email).
   - Automatic clean-up of usage counters after 13 months.
6. **Garmin branding.** Don't use Garmin's logos until you've read the Garmin brand guidelines; the text
   mention plus trademark notice in the footer is fine.

---

## Form answers

**Company Name:** Dominik Reuter – VibeGrind

**Company Website URL:** https://vibegrind.app

**Company Privacy Policy URL:** https://vibegrind.app/privacy

**Contact Name:** Dominik Reuter

**Job Title:** Founder

**Email address:** hello@vibegrind.app

**Address:** Zaystrasse 21 · **City:** Goldau · **Country:** Switzerland · **Postal Code:** 6410

**What is your company's primary business service?** Coaching & Training Programs

**What industry do you serve?** Personal Training & Coaching

**Provide a brief overview of your application, the user experience and how the APIs will be used.**

> VibeGrind is a web app for cyclists that turns plain-language requests ("4×8 minutes at threshold,
> about an hour") into structured workouts. Users see each workout as a zone-colored chart with
> duration, intensity factor and TSS, can refine it by chat, and export it for Zwift or as a Garmin
> workout file.
>
> With Garmin connected, VibeGrind shows a daily readiness dashboard and suggests workouts that fit
> the athlete's current state, for example an endurance ride instead of VO2 max intervals after a short
> night with HRV below baseline.
>
> - **Health API:** overnight HRV with baseline, resting heart rate, sleep duration/stages/score,
>   Body Battery and stress, used for the readiness estimate and 30-day trends.
> - **Activity API:** activity summaries (type, duration, power, heart rate, training load, training
>   effect), used for acute vs. chronic training load and the recent-activity list.
> - **Training API:** to send the workouts users create directly to their Garmin devices, replacing
>   today's manual .fit file transfer.
>
> Users connect Garmin explicitly via OAuth, give consent for their health data, and can disconnect
> and have their Garmin data deleted at any time. Data is only shown to the user it belongs to.
> The readiness dashboard and readiness-aware workout suggestions already run on the developer's own
> data through the Garmin Connect Personal API; Developer Program access would let other athletes
> connect their own accounts.

**Describe the business model for the products or services using this API access.**

> VibeGrind is in a free, invite-only beta. After the beta we plan a paid subscription for individual
> athletes (around `[CHF 5–8]` per month), with a limited free tier. There is no advertising, and we don't
> sell or share users' data; Garmin data is used only to provide the service to the user it belongs to.

**Please identify any third-party services, including analytics, machine learning or AI platforms, that will process, analyze or store Garmin device-sourced data.**

> - **Render (Render Services, Inc.):** application hosting and database; stores the Garmin data we
>   receive. Region: Frankfurt, Germany (EU).
> - **Anthropic (Claude API):** generates and adjusts workouts. When a user chats, we send a short
>   derived summary: today's readiness estimate and its components (HRV vs. baseline, resting heart
>   rate, sleep, Body Battery), the last 7 days of these values and of training load, and recent
>   activity summaries. Raw Garmin data isn't sent. Under Anthropic's commercial terms, API data isn't
>   used to train their models.
> - No analytics, advertising or tracking services are used.

**Which APIs are you interested in?** Health API, Activity API, Training API

**How many users do you expect to connect to your application within the next 12 months?** 0-100

**Have you already been in contact with Garmin or a Garmin distributor?** No
