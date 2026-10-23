"""
Tiny UI translation helper for the control panel.

The UI is written in English; tr("English text") returns the French version when
French is selected, and falls back to the English text if there is no entry — so
a missing translation never breaks anything.

To add a language: add it to LANG_NAMES and add a dict to TRANSLATIONS.
Dialect dicts (en-GB, en-US) only need the strings they change.
"""

import os
import locale

LANG_NAMES = {
    "en":    "International",
    "en-GB": "Traditional",
    "en-CA": "Canadian",
    "en-US": "AMERICAN",
    "fr":    "Français",
}
_lang = "en"


def detect_lang() -> str:
    """Best-effort guess from the OS language (used until the user picks one).
    Never picks the American joke dialect on its own — that one's opt-in only."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        tag = os.environ.get(var, "").split(".")[0].replace("_", "-").lower()
        if tag.startswith("fr"):
            return "fr"
        if tag == "en-gb":
            return "en-GB"
        if tag == "en-ca":
            return "en-CA"
    try:
        v = (locale.getlocale()[0] or "").replace("_", "-").lower()
        if v.startswith("fr"):
            return "fr"
        if v == "en-gb":
            return "en-GB"
        if v == "en-ca":
            return "en-CA"
    except Exception:
        pass
    return "en"


def set_lang(code: str):
    global _lang
    _lang = code if code in LANG_NAMES else "en"


def get_lang() -> str:
    return _lang


def tr(text: str, **fmt) -> str:
    out = TRANSLATIONS.get(_lang, {}).get(text, text)
    return out.format(**fmt) if fmt else out


TRANSLATIONS = {
    "en": {},
    "fr": {
        # window / tabs
        "systemc control panel": "panneau de contrôle systemc",
        "🦞  systemc control panel": "🦞  panneau de contrôle systemc",
        "1. Setup": "1. Configuration",
        "2. Dependencies": "2. Dépendances",
        "3. Run && Logs": "3. Exécution && Journaux",
        "4. Approvals": "4. Approbations",
        "API token": "Jeton d'API",
        "For scripts and other programs. The API is served by the Local web UI at http://127.0.0.1:7842/api/v1 (see API.md). Keep the token secret.":
            "Pour les scripts et autres programmes. L'API est servie par l'interface web locale sur http://127.0.0.1:7842/api/v1 (voir API.md). Garde le jeton secret.",
        "🔑 Generate": "🔑 Générer",
        "👁 Show": "👁 Afficher",
        "🙈 Hide": "🙈 Masquer",
        "📋 Copy": "📋 Copier",
        "Replace the API token?": "Remplacer le jeton d'API ?",
        "Anything using the current token will stop working.": "Tout ce qui utilise le jeton actuel cessera de fonctionner.",
        "✓ new API token saved": "✓ nouveau jeton d'API enregistré",
        "✓ copied": "✓ copié",
        "Couldn't save": "Impossible d'enregistrer",
        "The server owner can always use these tools. Add the Discord user IDs of anyone else who may (right-click a user → Copy User ID, with Developer Mode on).":
            "Le propriétaire du serveur peut toujours utiliser ces outils. Ajoute les ID Discord de toute autre personne autorisée (clic droit sur un utilisateur → Copier l'identifiant, mode développeur activé).",
        "That doesn't look like a Discord user ID (15-25 digits).":
            "Ça ne ressemble pas à un ID d'utilisateur Discord (15 à 25 chiffres).",
        "➕ Add": "➕ Ajouter",
        "Couldn't load discord_admin ({e}). Install the missing packages on the Dependencies tab, then press Rescan.":
            "Impossible de charger discord_admin ({e}). Installe les paquets manquants dans l'onglet Dépendances, puis appuie sur Réanalyser.",
        "Risky actions only (default)": "Actions risquées seulement (par défaut)",
        "Everything": "Tout",
        "Nothing": "Rien",
        "Ask for approval on:": "Demander approbation pour :",
        "Per-action settings below win over this. Select one or more actions, then choose a setting.":
            "Les réglages par action ci-dessous l'emportent. Sélectionne une ou plusieurs actions, puis choisis un réglage.",
        "Action": "Action",
        "Default": "Par défaut",
        "Setting": "Réglage",
        "asks": "demande",
        "doesn't ask": "ne demande pas",
        "always ask": "toujours demander",
        "never ask": "ne jamais demander",
        "default": "par défaut",
        "Skip approval?": "Se passer d'approbation ?",
        "These can be destructive and normally ask first:\n\n{names}\n\nLet the bot do them without asking?":
            "Ces actions peuvent être destructrices et demandent normalement d'abord :\n\n{names}\n\nLaisser le bot les faire sans demander ?",
        "🔒 Always ask": "🔒 Toujours demander",
        "🔓 Never ask": "🔓 Ne jamais demander",
        "↺ Use default": "↺ Par défaut",
        "5. Integrations": "5. Intégrations",
        "Integrations": "Intégrations",
        "↻ Rescan": "↻ Réanalyser",
        "Switch extra tool packs on or off. Changes are saved to .env (INTEGRATIONS) and take effect the next time the bot starts.":
            "Active ou désactive les modules d'outils supplémentaires. Les changements sont enregistrés dans .env (INTEGRATIONS) et s'appliquent au prochain démarrage du bot.",
        "●  enabled": "●  activée",
        "●  disabled": "●  désactivée",
        "▸ Settings": "▸ Réglages",
        "▾ Settings": "▾ Réglages",
        "🚧 Not wired up yet.": "🚧 Pas encore branché.",
        "No integrations were found in the integrations folder.":
            "Aucune intégration n'a été trouvée dans le dossier integrations.",
        "This integration's settings could not be read. Press Rescan.":
            "Les réglages de cette intégration sont illisibles. Appuie sur Réanalyser.",
        "Nothing changed.": "Rien n'a changé.",
        "Reset to defaults?": "Rétablir les valeurs par défaut ?",
        "Every setting on this page goes back to its default.":
            "Tous les réglages de cette page reviennent à leur valeur par défaut.",
        "💾 Save": "💾 Enregistrer",
        "↺ Reset page to defaults": "↺ Valeurs par défaut de la page",
        "Clear the saved value": "Effacer la valeur enregistrée",
        "A value is saved — type here to replace it. ": "Une valeur est enregistrée — saisis ici pour la remplacer. ",
        "Not set. ": "Non défini. ",
        "Separate items with commas. ": "Sépare les éléments par des virgules. ",
        " (default: {d})": " (par défaut : {d})",
        "✓ saved to .env — restart the bot to apply":
            "✓ enregistré dans .env — redémarre le bot pour appliquer",

        # setup tab — Discord
        "Discord bot token": "Token du bot Discord",
        "discord.com/developers/applications → your app → Bot → Reset Token.\nAlso turn on 'Message Content Intent' on that same page.":
            "discord.com/developers/applications → ton application → Bot → Reset Token.\nActive aussi « Message Content Intent » sur cette même page.",
        "Allowed channel ID(s)": "ID(s) de canal autorisé(s)",
        "Right-click a channel in Discord → Copy Channel ID (enable Developer Mode in Discord settings first). Comma-separate multiple.":
            "Clic droit sur un canal dans Discord → Copier l'identifiant du salon (active d'abord le mode développeur dans les paramètres de Discord). Sépare-les par des virgules.",
        "Bot mode": "Mode du bot",
        "Wake word / @mention": "Mot d'éveil / @mention",
        "@mention only": "@mention seulement",
        "Every message": "Chaque message",
        "Wake word": "Mot d'éveil",

        # setup tab — Stoat
        "Stoat (optional)": "Stoat (facultatif)",
        "Stoat is an open-source Discord alternative. Leave blank to skip.\nGet a bot token at stoat.chat → Settings → My Bots → New Bot.":
            "Stoat est une alternative open source à Discord. Laisse vide pour ignorer.\nObtiens un token de bot sur stoat.chat → Paramètres → My Bots → New Bot.",
        "Stoat bot token": "Token du bot Stoat",
        "Stoat channel ID(s)": "ID(s) de canal Stoat",
        "Stoat bot mode": "Mode du bot Stoat",
        "Stoat wake word": "Mot d'éveil Stoat",

        # setup tab — Ollama
        "Ollama source": "Source d'Ollama",
        "Local = an Ollama server on this machine. Ollama Cloud = run big models on ollama.com with an API key (no GPU needed).":
            "Local = un serveur Ollama sur cette machine. Ollama Cloud = exécute de gros modèles sur ollama.com avec une clé API (aucun GPU nécessaire).",
        "Local Ollama": "Ollama local",
        "Ollama Cloud (API key)": "Ollama Cloud (clé API)",
        "Ollama API keys": "Clés API Ollama",
        "Add one or more keys from https://ollama.com/settings/keys. They're used in order: if one hits a usage limit or is rejected, the next one takes over automatically. Use ▲ ▼ to choose which is the primary.":
            "Ajoute une ou plusieurs clés depuis https://ollama.com/settings/keys. Elles sont utilisées dans l'ordre : si l'une atteint sa limite d'usage ou est refusée, la suivante prend automatiquement le relais. Utilise ▲ ▼ pour choisir la clé principale.",
        "🗑 Remove": "🗑 Retirer",
        "➕ Add key": "➕ Ajouter la clé",
        "primary": "principale",
        "fallback": "secours",
        "Model": "Modèle",
        "Agent name": "Nom de l'agent",
        "What the agent calls itself. It's written into the system prompt, so the agent answers to this name. Leave blank for the default (SystemC). Restart the bot to apply.":
            "Le nom que l'agent se donne. Il est écrit dans le prompt système, l'agent répond donc à ce nom. Laisser vide pour le nom par défaut (SystemC). Redémarrez le bot pour appliquer.",
        "Needs tool calling (e.g. gpt-oss:120b, qwen3, llama3.1). Not sure of the exact ID? Click 'Fetch available models' and pick one from the list.":
            "Doit supporter l'appel d'outils (ex. gpt-oss:120b, qwen3, llama3.1). Tu ne connais pas l'identifiant exact ? Clique sur « Récupérer les modèles disponibles » et choisis dans la liste.",
        "🔎  Fetch available models": "🔎  Récupérer les modèles disponibles",
        "Agent workspace directory": "Dossier de travail de l'agent",
        "Agent workspace & account": "Espace de travail et compte de l'agent",
        "Configure where the agent stores files and runs shell commands.":
            "Configure où l'agent stocke ses fichiers et exécute les commandes shell.",
        "Run under another user account (e.g. 'agent')":
            "Exécuter sous un autre compte utilisateur (ex. « agent »)",
        "Runs shell commands as a dedicated system user (e.g. agent) via sudo/runuser.":
            "Exécute les commandes shell sous un utilisateur système dédié (ex. agent) via sudo/runuser.",
        "Local app workspace or custom directory":
            "Espace local de l'application ou dossier personnalisé",
        "Standard folder inside this app or a custom path of your choice.":
            "Dossier standard dans cette application ou chemin personnalisé de votre choix.",
        "Account username:": "Nom d'utilisateur :",
        "Workspace directory:": "Dossier de travail :",
        "⚡ Set up / Verify directory": "⚡ Configurer / Vérifier le dossier",
        "Agent $HOME and files live in /home/agent, owned by your account.":
            "Le $HOME et les fichiers de l'agent se trouvent dans /home/agent, détenus par votre compte.",
        "✓ Ready (owner: {owner})": "✓ Prêt (propriétaire : {owner})",
        "⚠️ Does not exist (root required to create)": "⚠️ N'existe pas (privilèges root requis pour créer)",
        "⚠️ Exists but not writable by {target}": "⚠️ Existe mais non modifiable par {target}",
        "⚠️ Needs setup / root privileges": "⚠️ Nécessite une configuration / privilèges root",
        "Workspace setup": "Configuration de l'espace de travail",
        "Workspace setup error": "Erreur de configuration de l'espace de travail",
        "Where the agent reads/writes files and runs shell commands from. Defaults to a 'workspace' folder next to this app.":
            "Là où l'agent lit/écrit des fichiers et exécute des commandes shell. Par défaut : un dossier « workspace » à côté de cette application.",
        "💾  Save config": "💾  Enregistrer la config",
        "fetching…": "récupération…",
        "✓ {n} model(s) — open the dropdown to choose": "✓ {n} modèle(s) — ouvre le menu déroulant pour choisir",
        "✓ saved to .env": "✓ enregistré dans .env",

        # dependencies tab
        "Python packages": "Paquets Python",
        "🔎  Re-check": "🔎  Revérifier",
        "⬇️  Install missing": "⬇️  Installer les manquants",
        "Ollama": "Ollama",
        "checking...": "vérification...",
        "🔎  Re-check Ollama": "🔎  Revérifier Ollama",
        "⬇️  Pull recommended model (qwen2.5)": "⬇️  Télécharger le modèle recommandé (qwen2.5)",
        "☁️  Ollama Cloud mode — no local Ollama needed (uses your API key)":
            "☁️  Mode Ollama Cloud — pas besoin d'Ollama local (utilise ta clé API)",
        "⚠️  ollama not found — install from https://ollama.com":
            "⚠️  ollama introuvable — installe-le depuis https://ollama.com",
        "⚠️  installed but not running — run: ollama serve":
            "⚠️  installé mais pas démarré — lance : ollama serve",
        "✓  running — {n} model(s) installed": "✓  en marche — {n} modèle(s) installé(s)",
        "⚠️  running, but no models pulled — run: ollama pull qwen2.5":
            "⚠️  en marche, mais aucun modèle téléchargé — lance : ollama pull qwen2.5",

        # run tab
        "Discord bot": "Bot Discord",
        "Local web UI": "Interface web locale",
        "Stoat bot": "Bot Stoat",
        "●  stopped": "●  arrêté",
        "●  running": "●  en cours",
        "▶ Start": "▶ Démarrer",
        "■ Stop": "■ Arrêter",
        "🌐 Open": "🌐 Ouvrir",
        "Auto-restart on crash": "Redémarrage auto en cas de plantage",
        "Logs": "Journaux",

        # approvals tab
        "Pending approvals": "Approbations en attente",
        "Destructive commands (rm, sudo, apt install, etc.) show up here too — approve or deny without needing Discord open.":
            "Les commandes destructrices (rm, sudo, apt install, etc.) apparaissent aussi ici — approuve ou refuse sans avoir besoin de Discord.",
        "✅ Approve selected": "✅ Approuver la sélection",
        "❌ Deny selected": "❌ Refuser la sélection",
        "Id": "ID",
        "Command": "Commande",
        "Age": "Âge",

        # dialogs
        "Missing token": "Token manquant",
        "Paste a Discord bot token and/or a Stoat bot token first.":
            "Colle d'abord un token de bot Discord et/ou un token de bot Stoat.",
        "Missing channel": "Canal manquant",
        "Enter at least one Discord channel ID first.": "Entre d'abord au moins un ID de canal Discord.",
        "Missing API key": "Clé API manquante",
        "Add your Ollama API key (https://ollama.com/settings/keys), or switch to Local Ollama.":
            "Ajoute ta clé API Ollama (https://ollama.com/settings/keys), ou passe à Ollama local.",
        "API key needed": "Clé API requise",
        "Add your Ollama API key first (https://ollama.com/settings/keys).":
            "Ajoute d'abord ta clé API Ollama (https://ollama.com/settings/keys).",
        "Nothing to do": "Rien à faire",
        "All required packages are already installed.": "Tous les paquets requis sont déjà installés.",
        "Working...": "En cours...",
        "Progress is streaming into the 'Run && Logs' tab.":
            "La progression s'affiche dans l'onglet « Exécution && Journaux ».",
        "Not configured": "Non configuré",
        "Add your Stoat bot token in the Setup tab first.":
            "Ajoute d'abord ton token de bot Stoat dans l'onglet Configuration.",
        "Save your config in the Setup tab first.":
            "Enregistre d'abord ta config dans l'onglet Configuration.",

        # sessions tab
        "6. Sessions": "6. Sessions",
        "Sessions": "Sessions",
        "↻ Refresh": "↻ Actualiser",
        "Every channel the bot talks in has its own conversation session. Resetting one wipes what was said in it, so the next message starts fresh. Long-term memory is not touched. Works whether or not the bots are running.":
            "Chaque canal où le bot parle a sa propre session de conversation. En réinitialiser une efface ce qui s'y est dit : le message suivant repart de zéro. La mémoire à long terme n'est pas touchée. Fonctionne que les bots tournent ou non.",
        "🧹 Reset selected": "🧹 Réinitialiser la sélection",
        "Platform": "Plateforme",
        "Channel": "Canal",
        "Turns": "Échanges",
        "Last active": "Dernière activité",
        "Last message": "Dernier message",
        "Select one or more sessions first.": "Sélectionne d'abord une ou plusieurs sessions.",
        "Reset sessions": "Réinitialiser les sessions",
        "Reset {n} session(s)? Their conversation history will be erased. Long-term memory is not affected.":
            "Réinitialiser {n} session(s) ? Leur historique de conversation sera effacé. La mémoire à long terme n'est pas touchée.",
        "✓ {n} session(s) reset": "✓ {n} session(s) réinitialisée(s)",
        "✓ {done} reset, {failed} failed (check permissions on .history/)":
            "✓ {done} réinitialisée(s), {failed} en échec (vérifie les permissions de .history/)",
    },
}


# ── English dialects ─────────────────────────────────────────────────────────
# "en" (International) is the base text itself. Traditional (UK) tweaks a few
# strings; Canadian is, per the user's own analysis, the UK set. American is a
# full, loving, over-the-top parody of every string.

_EN_GB = {
    "Stoat (optional)": "Stoat (if you fancy)",
    "⬇️  Install missing": "⬇️  Install what's missing",
    "checking...": "having a look...",
    "Auto-restart on crash": "Restart automatically if it falls over",
    "✓ saved to .env": "✓ saved to .env — lovely",
    "Pending approvals": "Awaiting approval",
    "Missing token": "Token missing",
    "Missing channel": "Channel missing",
    "Nothing to do": "Nothing to do — all sorted",
    "All required packages are already installed.": "All required packages are already installed. All sorted.",
    "Working...": "Hang on a mo...",
    "Not configured": "Not yet configured",
}

_EN_US = {
    "systemc control panel": "SYSTEMC CONTROL PANEL",
    "🦞  systemc control panel": "🦅  SYSTEMC CONTROL PANEL  🦅",
    "1. Setup": "1. SETUP 🦅",
    "2. Dependencies": "2. INDEPENDENCE",
    "3. Run && Logs": "3. LAUNCH && LOGS",
    "4. Approvals": "4. APPROVALS 🦅",
    "5. Integrations": "5. INTEGRATIONS 🦅",
    "Integrations": "INTEGRATIONS (FREEDOM PACKS)",
    "●  enabled": "●  ENABLED (LOCKED AND LOADED)",
    "●  disabled": "●  DISABLED",
    "🚧 Not wired up yet.": "🚧 NOT WIRED UP YET (COMING SOON TO A THEATER NEAR YOU).",

    "Discord bot token": "DISCORD BOT TOKEN (GUARD IT LIKE FORT KNOX)",
    "discord.com/developers/applications → your app → Bot → Reset Token.\nAlso turn on 'Message Content Intent' on that same page.":
        "discord.com/developers/applications → YOUR app → Bot → Reset Token. Land of the free, home of the token reset.\nAlso flip the 'Message Content Intent' switch on that same page, like a true patriot.",
    "Allowed channel ID(s)": "ALLOWED CHANNEL ID(S) (THE FREE CHANNELS)",
    "Right-click a channel in Discord → Copy Channel ID (enable Developer Mode in Discord settings first). Comma-separate multiple.":
        "Right-click a channel in Discord → Copy Channel ID (enable Developer Mode in Discord settings first — it's a free country). Separate multiples with commas. Freedom of choice!",
    "Bot mode": "BOT MODE (CHOOSE YOUR DESTINY)",
    "Wake word / @mention": "WAKE WORD / @MENTION 🦅",
    "@mention only": "@MENTION ONLY",
    "Every message": "EVERY MESSAGE (FULL SEND)",
    "Wake word": "WAKE WORD (RISE AND SHINE, AMERICA)",

    "Stoat (optional)": "STOAT (OPTIONAL, NOT AN EAGLE)",
    "Stoat is an open-source Discord alternative. Leave blank to skip.\nGet a bot token at stoat.chat → Settings → My Bots → New Bot.":
        "Stoat is an open-source Discord alternative (freedom of software!). Leave blank to skip.\nGet a bot token at stoat.chat → Settings → My Bots → New Bot.",
    "Stoat bot token": "STOAT BOT TOKEN",
    "Stoat channel ID(s)": "STOAT CHANNEL ID(S)",
    "Stoat bot mode": "STOAT BOT MODE",
    "Stoat wake word": "STOAT WAKE WORD",

    "Ollama source": "OLLAMA SOURCE (DOMESTIC OR IMPORTED?)",
    "Local = an Ollama server on this machine. Ollama Cloud = run big models on ollama.com with an API key (no GPU needed).":
        "Local = an Ollama server right here on American soil (your machine). Ollama Cloud = run big models on ollama.com with an API key (no GPU needed, still free-range).",
    "Local Ollama": "DOMESTIC OLLAMA (LOCAL)",
    "Ollama Cloud (API key)": "OLLAMA CLOUD (API KEY) ☁️🦅",
    "Ollama API keys": "OLLAMA API KEYS (THE KEYS TO FREEDOM)",
    "Add one or more keys from https://ollama.com/settings/keys. They're used in order: if one hits a usage limit or is rejected, the next one takes over automatically. Use ▲ ▼ to choose which is the primary.":
        "Add one or more keys from https://ollama.com/settings/keys. They're used in order: if one hits a usage limit or is rejected, the next one steps up like a true backup quarterback. Use ▲ ▼ to pick the starting lineup.",
    "🗑 Remove": "🗑 REMOVE",
    "➕ Add key": "➕ ADD KEY",
    "primary": "starter",
    "fallback": "backup",
    "Model": "CHOOSE YOUR TRULY AMERICAN MODEL 🦅🦅🦅🦅🦅",
    "Needs tool calling (e.g. gpt-oss:120b, qwen3, llama3.1). Not sure of the exact ID? Click 'Fetch available models' and pick one from the list.":
        "Must support tool calling (e.g. gpt-oss:120b, qwen3, llama3.1). Don't know the exact ID? Hit the big button below and pick your champion from the list.",
    "🔎  Fetch available models": "🔎  FETCH MODELS, AMERICAN-STYLE",
    "Agent workspace directory": "AGENT WORKSPACE DIRECTORY (HOME OF THE BRAVE)",
    "Where the agent reads/writes files and runs shell commands from. Defaults to a 'workspace' folder next to this app.":
        "Where the agent reads/writes files and runs shell commands from. Defaults to a 'workspace' folder next to this app — its own little homestead.",
    "💾  Save config": "💾  SAVE CONFIG 🎆",
    "fetching…": "fetching freedom…",
    "✓ {n} model(s) — open the dropdown to choose":
        "✓ {n} MODEL(S) LOCKED AND LOADED — OPEN THE DROPDOWN, PICK YOUR CHAMPION",
    "✓ saved to .env": "✓ saved to .env — MISSION ACCOMPLISHED",

    "Python packages": "PYTHON PACKAGES (IMPORTED FREEDOM)",
    "🔎  Re-check": "🔎  RE-CHECK",
    "⬇️  Install missing": "⬇️  INSTALL MISSING — LET FREEDOM RING",
    "Ollama": "OLLAMA 🦙🦅",
    "checking...": "inspecting for freedom...",
    "🔎  Re-check Ollama": "🔎  RE-CHECK OLLAMA",
    "⬇️  Pull recommended model (qwen2.5)": "⬇️  PULL RECOMMENDED MODEL (qwen2.5 — IMPORTED, BUT WE ALLOW IT)",
    "☁️  Ollama Cloud mode — no local Ollama needed (uses your API key)":
        "☁️  OLLAMA CLOUD MODE — NO LOCAL OLLAMA NEEDED (USES YOUR API KEY) 🦅",
    "⚠️  ollama not found — install from https://ollama.com":
        "⚠️  OLLAMA NOT FOUND — A BALD EAGLE WEEPS. INSTALL FROM https://ollama.com",
    "⚠️  installed but not running — run: ollama serve":
        "⚠️  INSTALLED BUT NOT RUNNING — RUN: ollama serve (RISE UP!)",
    "✓  running — {n} model(s) installed": "✓  RUNNING — {n} MODEL(S) INSTALLED 🦅",
    "⚠️  running, but no models pulled — run: ollama pull qwen2.5":
        "⚠️  RUNNING, BUT NO MODELS PULLED — RUN: ollama pull qwen2.5",

    "Discord bot": "DISCORD BOT",
    "Local web UI": "LOCAL WEB UI (HOME-GROWN)",
    "Stoat bot": "STOAT BOT",
    "●  stopped": "●  STANDING DOWN",
    "●  running": "●  LOCKED AND LOADED",
    "▶ Start": "▶ LAUNCH 🚀",
    "■ Stop": "■ STAND DOWN",
    "🌐 Open": "🌐 OPEN",
    "Auto-restart on crash": "AUTO-RESTART ON CRASH (AMERICANS NEVER QUIT)",
    "Logs": "LOGS (THE OFFICIAL RECORD)",

    "Pending approvals": "PENDING APPROVALS (AWAITING RATIFICATION)",
    "Destructive commands (rm, sudo, apt install, etc.) show up here too — approve or deny without needing Discord open.":
        "Destructive commands (rm, sudo, apt install, etc.) show up here too — approve or deny without needing Discord open. Democracy in action!",
    "✅ Approve selected": "✅ APPROVE SELECTED (YEA)",
    "❌ Deny selected": "❌ DENY SELECTED (NAY)",
    "Id": "ID",
    "Command": "COMMAND",
    "Age": "AGE",

    "Missing token": "TOKEN MISSING IN ACTION",
    "Paste a Discord bot token and/or a Stoat bot token first.":
        "Paste a Discord bot token and/or a Stoat bot token first. Nobody crosses this border without a token!",
    "Missing channel": "CHANNEL MISSING IN ACTION",
    "Enter at least one Discord channel ID first.": "Enter at least one Discord channel ID first, citizen.",
    "Missing API key": "API KEY MISSING IN ACTION",
    "Add your Ollama API key (https://ollama.com/settings/keys), or switch to Local Ollama.":
        "Add your Ollama API key (https://ollama.com/settings/keys), or switch to DOMESTIC Ollama (Local).",
    "API key needed": "API KEY REQUIRED, PATRIOT",
    "Add your Ollama API key first (https://ollama.com/settings/keys).":
        "Add your Ollama API key first (https://ollama.com/settings/keys).",
    "Nothing to do": "NOTHING TO DO (MISSION ACCOMPLISHED)",
    "All required packages are already installed.": "All required packages are already installed. Freedom is fully stocked.",
    "Working...": "WORKING... (HUSTLING)",
    "Progress is streaming into the 'Run && Logs' tab.":
        "Progress is streaming live into the 'LAUNCH && LOGS' tab, like the big game.",
    "Not configured": "NOT CONFIGURED (YET)",
    "Add your Stoat bot token in the Setup tab first.": "Add your Stoat bot token in the SETUP tab first.",
    "Save your config in the Setup tab first.": "Save your config in the SETUP tab first.",

    "6. Sessions": "6. SESSIONS 🦅",
    "Sessions": "SESSIONS (CONVERSATIONS OF THE PEOPLE)",
    "↻ Refresh": "↻ REFRESH",
    "Every channel the bot talks in has its own conversation session. Resetting one wipes what was said in it, so the next message starts fresh. Long-term memory is not touched. Works whether or not the bots are running.":
        "Every channel the bot talks in has its own conversation session. Resetting one wipes the slate clean, so the next message is a fresh start — the American dream. Long-term memory is untouched. Works whether or not the bots are running. Freedom doesn't clock out!",
    "🧹 Reset selected": "🧹 RESET SELECTED (FRESH START, AMERICAN-STYLE)",
    "Platform": "PLATFORM",
    "Channel": "CHANNEL",
    "Turns": "TURNS",
    "Last active": "LAST ACTIVE",
    "Last message": "LAST MESSAGE",
    "Select one or more sessions first.": "Select one or more sessions first, citizen.",
    "Reset sessions": "RESET SESSIONS",
    "Reset {n} session(s)? Their conversation history will be erased. Long-term memory is not affected.":
        "Reset {n} session(s)? Their conversation history will be erased. Long-term memory is safe — we protect what matters.",
    "✓ {n} session(s) reset": "✓ {n} SESSION(S) RESET — FRESH START, AMERICAN-STYLE 🦅",
    "✓ {done} reset, {failed} failed (check permissions on .history/)":
        "✓ {done} RESET, {failed} FAILED (check permissions on .history/)",
}

TRANSLATIONS["en-GB"] = _EN_GB
TRANSLATIONS["en-CA"] = _EN_GB      # Canadian: literally the UK words (see above)
TRANSLATIONS["en-US"] = _EN_US


# ── emoji-free twins ─────────────────────────────────────────────────────────
# The control panel shows its labels without emoji (except on the Integrations tab).
# Every translation whose English text contains an emoji also gets a twin without it,
# so those labels stay translated.
import re as _re

_EMOJI = _re.compile("(?![✓✗★])[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2194-\u21AA\u2300-\u23FF\uFE0F\u200D]+[ \u00a0]*")


def _without_emoji(text: str) -> str:
    return _EMOJI.sub("", text).strip()


for _table in TRANSLATIONS.values():
    for _k, _v in list(_table.items()):
        _nk = _without_emoji(_k)
        if _nk and _nk != _k and _nk not in _table:
            _table[_nk] = _without_emoji(_v)
        elif _nk == _k:                      # plain label whose translation adds emoji (en-US jokes)
            _table[_k] = _without_emoji(_v)
