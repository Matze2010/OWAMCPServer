# Exchange E-Mail Tool für Open WebUI

Ein Workspace-Tool für [Open WebUI](https://docs.openwebui.com/), mit dem ein Sprachmodell
E-Mails über einen **lokalen (on-premises) Microsoft-Exchange-Server per EWS** versenden kann.
Technische Basis ist [exchangelib](https://github.com/ecederstrand/exchangelib).

Jeder Nutzer hinterlegt seine **eigenen** Postfach-Zugangsdaten; gesendet wird als dieser Nutzer
selbst (`access_type=DELEGATE`). Serverbezogene Einstellungen und Sicherheits-Flags verwaltet
ausschließlich der Administrator.

## Überblick

Das Tool stellt dem Modell zwei Funktionen bereit:

| Funktion | Zweck |
|---|---|
| `send_email` | E-Mail versenden: To/Cc/Bcc, Betreff, Text- oder HTML-Body, Wichtigkeit, Reply-To |
| `check_exchange_connection` | Verbindung und Zugangsdaten prüfen, ohne etwas zu senden |

**Nicht enthalten:** Anhänge, Lesen oder Empfangen von Mail, Kalender, Kontakte, Impersonation
über ein Dienstkonto.

**Zwei Schutzmechanismen sind standardmäßig aktiv:**

- Vor jedem echten Versand erscheint eine **Bestätigungsabfrage** im Chat. Ohne ausdrückliche
  Bestätigung wird nichts gesendet.
- Der **Trockenlauf** (`dry_run`) validiert alles, verbindet sich aber nie und sendet nie.

## Voraussetzungen

- Open WebUI mit aktivierten Workspace-Tools
- Ein erreichbarer Exchange-Server mit aktiviertem EWS (Exchange 2013 oder neuer)
- Persönliche Postfach-Zugangsdaten je Nutzer
- Netzwerkzugang des Open-WebUI-Containers zu PyPI für die Installation von `exchangelib`

## Installation

1. Den gesamten Inhalt von `exchange_email_tool.py` kopieren.
2. In Open WebUI: **Workspace → Tools → „+"**.
3. Code einfügen und speichern. Open WebUI liest den Metadaten-Header und installiert
   `exchangelib` automatisch.
4. Das Tool im gewünschten Modell bzw. im Chat aktivieren.

> **Hinweis:** Die automatische Installation aus dem `requirements:`-Header lässt sich in Open WebUI
> über `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` abschalten, und in Offline-Umgebungen schlägt
> sie fehl. Ist `exchangelib` nicht vorhanden, meldet das Tool das im Klartext, statt zu scheitern —
> ein Administrator muss das Paket dann im Container nachinstallieren.

## Konfiguration: Valves (Administrator)

Zu finden unter **Workspace → Tools → *dieses Tool* → Valves**.

| Valve | Typ | Standard | Bedeutung |
|---|---|---|---|
| `ews_server` | Text | – | Hostname des Exchange-Servers, z. B. `mail.example.com`. |
| `ews_service_endpoint` | Text | – | Vollständige EWS-URL, z. B. `https://mail.example.com/EWS/Exchange.asmx`. **Hat Vorrang vor `ews_server`.** |
| `auth_type` | Auswahl | `NTLM` | `NTLM`, `BASIC`, `GSSAPI`, `SSPI` oder `CBA`. NTLM passt für die meisten On-Prem-Server. |
| `autodiscover` | Ja/Nein | `Nein` | Autodiscover statt fester Serveradresse. Langsamer und intern oft blockiert. |
| `exchange_build` | Text | – | Exchange-Build festnageln, z. B. `15.1.2507.16`. Spart die Versionserkennung pro Verbindung. |
| `verify_ssl` | Ja/Nein | `Ja` | TLS-Zertifikat prüfen. **Wirkt prozessweit** — siehe Warnung unten. |
| `ca_bundle_path` | Text | – | Pfad zu einem CA-Bundle (PEM) für interne Zertifizierungsstellen. **Wirkt prozessweit.** |
| `request_timeout` | Zahl | `60` | Timeout je EWS-Anfrage in Sekunden. **Wirkt prozessweit.** |
| `require_confirmation` | Ja/Nein | `Ja` | Rückfrage im Chat vor jedem echten Versand. |
| `dry_run` | Ja/Nein | `Nein` | Simulationsmodus: validieren, aber nie verbinden und nie senden. |
| `save_to_sent_items` | Ja/Nein | `Ja` | Kopie im Ordner „Gesendete Elemente" ablegen. |
| `allowed_recipient_domains` | Text | – | Kommaliste erlaubter Empfängerdomänen. Leer = alle erlaubt. |
| `blocked_recipient_domains` | Text | – | Kommaliste gesperrter Domänen. **Schlägt die Allowlist.** |
| `max_recipients` | Zahl | `25` | Obergrenze über To + Cc + Bcc zusammen. |
| `auto_detect_html` | Ja/Nein | `Ja` | Body als HTML behandeln, wenn er erkennbar Markup enthält. |
| `emit_status` | Ja/Nein | `Ja` | Fortschrittsmeldungen im Chat anzeigen. |
| `debug_errors` | Ja/Nein | `Nein` | Technischen Exception-Typ und -Text an Fehlermeldungen anhängen. |

### Serveradresse: `ews_server` oder `ews_service_endpoint`

Genau **eines** von beiden setzen:

- `ews_server = mail.example.com` → das Tool bildet daraus `https://mail.example.com/EWS/Exchange.asmx`.
- `ews_service_endpoint = https://…/EWS/Exchange.asmx` → wird unverändert genutzt und **überschreibt**
  `ews_server`.

Beide Werte gleichzeitig an exchangelib zu übergeben, würde einen Fehler auslösen; das Tool
verhindert das und nutzt in diesem Fall den Endpoint. Sind beide leer, muss `autodiscover` aktiv sein.

### ⚠️ Prozessweite Einstellungen

`verify_ssl`, `ca_bundle_path` und `request_timeout` werden von exchangelib als
Klassenattribute abgelegt. Sie wirken deshalb auf **den gesamten Open-WebUI-Prozess**, nicht nur auf
dieses Tool, und greifen zuverlässig nur, **bevor die erste Verbindung aufgebaut wurde**.

Nach einer Änderung dieser drei Werte Open WebUI neu starten. Das Tool erkennt eine nachträgliche
Änderung und hängt einen entsprechenden Warnhinweis an sein Ergebnis an.

## Konfiguration: UserValves (jeder Nutzer selbst)

Zu finden im Chat unter **Controls → Valves** bzw. in den persönlichen Tool-Einstellungen.

| Valve | Standard | Bedeutung |
|---|---|---|
| `enabled` | `Ja` | Persönlicher Schalter: Darf das Tool in meinem Namen senden? |
| `username` | – | Exchange-Anmeldename, meist `DOMAIN\benutzername` (**einfacher** Backslash). |
| `email_address` | – | Eigene primäre SMTP-Adresse — das Absenderpostfach. |
| `password` | – | Exchange-Passwort. |
| `signature` | – | Optionale Klartext-Signatur, wird an jede Mail angehängt. |

### 🔒 Sicherheitshinweis zum Passwort

Open WebUI speichert Valve-Werte als JSON in seiner Datenbank. Es gibt **keinen** gesonderten
Secret-Feldtyp — das Passwort ist als **im Klartext gespeichert** zu betrachten. Entsprechend
sollte bewusst entschieden werden, welches Konto hier hinterlegt wird und wer Zugriff auf die
Open-WebUI-Datenbank hat.

Das Tool selbst gibt das Passwort nirgends aus: jede Rückgabe, jede Statusmeldung und jede
Fehlermeldung wird vor der Ausgabe geschwärzt, und es wird nicht protokolliert.

## Verwendung

Beispiel-Prompts:

> Schreib eine Mail an anna.beispiel@example.com mit dem Betreff „Protokoll Jour Fixe" und fasse
> unsere Punkte von eben zusammen.

> Sende die Zusammenfassung an team@example.com, mit max@example.com in CC, als HTML und mit hoher
> Wichtigkeit.

Hinweise:

- **Empfänger** werden kommagetrennt angegeben (`a@example.com, b@example.com`). Semikolon,
  Zeilenumbrüche und die Form `Name <a@example.com>` werden ebenfalls verstanden.
- **HTML vs. Klartext:** Das Modell kann `body_is_html` setzen. Vergisst es das, erkennt das Tool
  offensichtliches HTML von selbst (`auto_detect_html`) und weist im Ergebnis darauf hin.
  Aus HTML-Bodys werden `<script>`, `<iframe>`, `on…=`-Attribute und `javascript:`-Links immer
  entfernt.
- **Wichtigkeit:** `Low`, `Normal` oder `High`. Andere Angaben werden auf `Normal` zurückgesetzt —
  sichtbar, nicht stillschweigend.
- **Bcc:** In der Erfolgsmeldung erscheinen Bcc-Adressen nur als **Anzahl**. Sie zurück in das
  Chat-Protokoll zu schreiben, würde den Zweck von Bcc aufheben. Im Trockenlauf werden sie
  vollständig angezeigt, da nichts zugestellt wurde.

## Bestätigung vor dem Versand

Solange `require_confirmation` aktiv ist, erscheint vor jedem echten Versand ein Dialog mit
Absender, Empfängern (Bcc als Anzahl), Betreff und Format. Erst nach ausdrücklicher Bestätigung
baut das Tool überhaupt eine Verbindung zum Exchange-Server auf.

Das Verhalten ist bewusst **fail-closed**:

| Situation | Ergebnis |
|---|---|
| Nutzer bestätigt | Mail wird gesendet |
| Nutzer lehnt ab | Nichts wird gesendet |
| Dialog läuft in einen Timeout oder der Client ist getrennt | **Nichts wird gesendet** |
| Die Chat-Sitzung kann keinen Dialog anzeigen | **Nichts wird gesendet**, mit erklärender Meldung |

`require_confirmation = Nein` schaltet die Rückfrage ab. Das ist nur vertretbar, wenn unbeaufsichtigter
Versand ausdrücklich gewünscht ist — in Verbindung mit `allowed_recipient_domains` und einem
niedrigen `max_recipients`.

Im Trockenlauf entfällt die Rückfrage, weil ohnehin nichts gesendet wird.

## Testbetrieb (Trockenlauf)

Ist `dry_run` aktiv, führt `send_email` die **vollständige Validierung** durch — Zugangsdaten
vorhanden, Adressen gültig, Domain-Policy, Empfängerlimit, Betreff und Body — und gibt anschließend
eine Vorschau der Nachricht zurück. Es wird **keine Verbindung** aufgebaut und **nichts gesendet**.

```
DRY RUN - NO EMAIL WAS SENT
The tool is in simulation mode (valve 'dry_run' is enabled). No connection to Exchange was
made and nothing was delivered.

The following message WOULD have been sent:
From:       alice@example.com
To:         bob@example.com
...
DRY RUN - NO EMAIL WAS SENT. Disable the 'dry_run' valve to send for real.
```

Wichtig zu wissen:

- Der Trockenlauf prüft **keine** Zugangsdaten, kein TLS und keine Erreichbarkeit. Dafür ist
  `check_exchange_connection` da.
- `check_exchange_connection` **ignoriert `dry_run` bewusst** und verbindet sich immer wirklich —
  sonst könnte ein Administrator den Zustand des Tools falsch einschätzen. Das Ergebnis weist
  darauf hin, wenn `dry_run` aktiv ist.
- Der Trockenlauf funktioniert auch, wenn `exchangelib` noch nicht installiert ist. So lässt sich
  die Richtlinien-Konfiguration prüfen, bevor die Abhängigkeit steht.

**Empfohlener Ablauf für die Erstinbetriebnahme:**

1. `dry_run` einschalten, Server-Valves setzen, eigene UserValves füllen.
2. Das Modell eine Testmail schreiben lassen → Vorschau und Richtlinien prüfen.
3. `check_exchange_connection` aufrufen → Zugangsdaten, TLS und Endpoint prüfen.
4. `dry_run` ausschalten → beim ersten echten Versand erscheint der Bestätigungsdialog.

## Sicherheit und Richtlinien

- **Domain-Listen:** Der Vergleich ist **exakt** und ohne Subdomain-Aufweitung. `example.com`
  erlaubt weder `mail.example.com` noch `evil-example.com`. Subdomains müssen einzeln eingetragen
  werden. Die Blockliste wird zuerst geprüft und gewinnt immer.
- **Ganze Nachricht oder gar nichts:** Verstößt auch nur ein Empfänger gegen die Richtlinie oder ist
  eine Adresse ungültig, wird die komplette Nachricht abgelehnt. Empfänger stillschweigend zu
  entfernen und trotzdem zu senden wäre gefährlicher als ein klarer Fehler.
- **Prompt Injection:** Über den Inhalt entscheidet das Modell, und dessen Kontext kann
  fremdbestimmte Inhalte enthalten. Bestätigungsabfrage, Domain-Richtlinie, `max_recipients` und
  `dry_run` sind die Schutzmechanismen dagegen; HTML wird zusätzlich von aktiven Inhalten befreit.

## Fehlerbehebung

| Meldung / Symptom | Ursache und Abhilfe |
|---|---|
| `Authentication failed` | Benutzername im NTLM-Format `DOMAIN\benutzer` angeben. `auth_type` prüfen. Achtung: wiederholte Fehlversuche können das Konto sperren. |
| Zertifikatsfehler, selbstsigniertes Zertifikat | Bevorzugt `ca_bundle_path` auf das interne CA-Bundle setzen. `verify_ssl = Nein` nur als letztes Mittel — es wirkt prozessweit. Danach Open WebUI neu starten. |
| `Autodiscover could not locate the EWS endpoint` | `autodiscover` abschalten und `ews_service_endpoint` explizit setzen, üblicherweise `https://<server>/EWS/Exchange.asmx`. |
| `The server returned an unexpected response` | Die konfigurierte URL zeigt nicht auf einen EWS-Endpoint. Pfad prüfen. |
| `Exchange denied access` beim Senden | Möglicherweise ist der Ordner „Gesendete Elemente" nicht zugänglich. `save_to_sent_items` abschalten und erneut versuchen. Es wurde in diesem Fall nichts gesendet. |
| `You are not allowed to send as this address` | `email_address` passt nicht zu einem Postfach, aus dem `username` senden darf. |
| Zeitüberschreitungen | `request_timeout` erhöhen und Open WebUI neu starten. |
| `exchangelib is not available` | `requirements:`-Header im Tool prüfen und ob der Open-WebUI-Container PyPI erreicht. |
| Altes Passwort funktioniert noch | exchangelib hält Verbindungen prozessweit im Cache. Der alte Eintrag verfällt erst mit einem Neustart von Open WebUI. |
| Keine Bestätigungsabfrage, aber es wird gesendet | `require_confirmation` ist deaktiviert. |

Für die Diagnose lässt sich `debug_errors` einschalten: dann hängt das Tool Exception-Typ und
-Text an die Fehlermeldung an — geschwärzt, das Passwort erscheint auch dann nicht.

## Entwicklung und Tests

Die Testsuite läuft vollständig **ohne echten Exchange-Server**; `Account` und `Message` werden
durch aufzeichnende Doubles ersetzt.

```bash
uv sync --group dev
uv run pytest          # 144 Tests
uv run ruff check .
uv run ruff format --check .
```

`pyproject.toml` enthält bewusst keine Laufzeit-Abhängigkeiten: die Installation von `exchangelib`
übernimmt Open WebUI anhand des `requirements:`-Headers in der Tool-Datei. Die Datei dient nur der
lokalen Entwicklung.

## Bekannte Einschränkungen

- Keine Anhänge.
- Kein Lesen, Suchen oder Empfangen von Mail; kein Kalender, keine Kontakte.
- Kein Impersonation-Modus über ein Dienstkonto — jeder Nutzer sendet mit eigenen Zugangsdaten.
- `verify_ssl`, `ca_bundle_path` und `request_timeout` wirken prozessweit und erfordern nach einer
  Änderung einen Neustart.
- Der Anzeigename des Absenders stammt aus dem Exchange-Verzeichnis und lässt sich nicht überschreiben.

## Lizenz

MIT
