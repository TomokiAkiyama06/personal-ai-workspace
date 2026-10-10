// チャット (a later issue) with the Inferred Preference confirmation card (issue
// #38). There is no chat API or message list yet, so the screen keeps the
// placeholder and shows under it the card the chat would show under the latest
// assistant reply: at most one, the strongest ready candidate (the human's
// choice). When the chat arrives, it renders <PreferencePrompt> under each
// assistant reply with the conversation's and the reply's ids.
import { type MessageKey, useI18n } from "../i18n";
import { PreferencePrompt } from "../preferences/PreferencePrompt";
import { usePreferences } from "../preferences/store";

/** The conversation the placeholder stands for (one per tab until chats exist). */
const PLACEHOLDER_CONVERSATION = "placeholder";

export function ChatPage({ screen }: { screen: MessageKey }) {
  const { t } = useI18n();
  const preferences = usePreferences();
  return (
    <div className="page chat-page">
      <h1>{t("placeholder.title", { screen: t(screen) })}</h1>
      <p className="muted">{t("placeholder.body")}</p>
      {preferences && (
        <>
          <p className="muted small">{t("pref.chat.unavailable")}</p>
          <div className="chat-thread">
            <PreferencePrompt conversation={PLACEHOLDER_CONVERSATION} reply="latest" />
          </div>
        </>
      )}
    </div>
  );
}
