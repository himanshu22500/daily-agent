# Connecting Instagram (one time)

daily-agent reads Instagram accounts through the official Instagram Graph API
(Business Discovery), on behalf of **your own** Instagram professional account.
That needs four things, done once. Meta changes its menus often, so the names
below may differ a little.

## 1. Make your Instagram account a professional account

In the Instagram app: **Settings → Account type and tools → Switch to
professional account**, then pick **Creator** or **Business**. It's free and
you can switch back later. Your account doesn't need to post anything.

## 2. Link it to a Facebook Page

Business Discovery only works for an Instagram account linked to a Facebook
Page. Use any Page you manage, or create an empty one.

- In Instagram: **Edit profile → Page → Connect or create**, or
- on Facebook: open the Page → **Settings → Linked accounts → Instagram**.

## 3. Create a Meta app

1. Go to <https://developers.facebook.com/apps> and choose **Create app**.
2. When it asks for a use case, pick the one for managing Instagram/Pages (or
   **Other**), and choose the **Business** app type.
3. Add the **Instagram** product and choose the **API setup with Facebook
   login** variant.
4. Open **App settings → Basic** and note the **App ID** and **App secret**.

Leave the app in **Development** mode. That's enough, because only you (an app
admin) use it, and nothing needs Meta's review.

## 4. Get a token from the Graph API Explorer

1. Open <https://developers.facebook.com/tools/explorer>.
2. Pick your app under **Meta App**, and **User Token** under User or Page.
3. Add these permissions: `instagram_basic`, `instagram_manage_insights`,
   `pages_read_engagement`, `pages_show_list`.
   - If your Page is owned through a Business portfolio (Business Manager),
     also add `business_management` and `ads_read`.
4. Choose **Generate Access Token**. In the dialog, select your Page **and**
   your Instagram account.
5. Copy the token. It's short-lived (about an hour), so do the next step right
   away.

## 5. Run `instagram connect`

In the live install (`~/workspace/personal/daily-agent`):

```bash
uv run daily-agent instagram connect
```

Paste the App ID, the App secret and the token when asked. They aren't shown
or printed. The command:

1. swaps the token for a long-lived one,
2. finds the Page linked to your Instagram account and takes the **Page's**
   token, which doesn't expire,
3. checks that it can read your own account, and
4. saves `DAILY_AGENT_INSTAGRAM_TOKEN` and `DAILY_AGENT_INSTAGRAM_USER_ID` to
   `.env`. The App secret isn't saved.

Then restart the feed server:

```bash
launchctl kickstart -k gui/$(id -u)/com.daily-agent.web
```

Now paste `instagram.com/<name>` links on the Sources page.

## Troubleshooting

- **"No Facebook Page with a linked Instagram professional account"**: the
  Page isn't linked (step 2), or you didn't select the Page and the Instagram
  account in the token dialog (step 4).
- **"Instagram has no public Business or Creator account named @…"**: that
  account is personal or private (or the name is wrong). Only Business and
  Creator accounts can be followed.
- **"Instagram rejected the saved token"**: the token was revoked, for
  example because you changed your Facebook password or removed the app. Run
  `instagram connect` again.
- To disconnect, delete the two `DAILY_AGENT_INSTAGRAM_*` lines from `.env`,
  restart the feed server, and remove the app at
  <https://www.facebook.com/settings?tab=business_tools>.
