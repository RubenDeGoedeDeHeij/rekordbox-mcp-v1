# rekordbox-mcp

Lokale MCP-server (officiële `mcp` Python SDK, stdio) om een **Rekordbox 6/7**-bibliotheek
vanuit Claude te beheren: playlists, DJ-sets, genres/mappen, dedupe, MySettings, en
(experimenteel) cue-punten en beatgrids. Gebouwd op [pyrekordbox](https://github.com/dylanljones/pyrekordbox).

> ⚠️ Rekordbox' databaseformaat is reverse-engineered, niet officieel gedocumenteerd.
> Daarom gaat **elke** schrijfactie door een verplichte veiligheidslaag (zie hieronder).

## Veiligheidsmodel (geldt voor elke schrijftool)

1. **Dry-run is de standaard.** Elke schrijftool heeft `dry_run=true` (of `mode="dry_run"`) als default
   en laat zien wat er zou gebeuren. Pas met `dry_run=false` / `mode="execute"` wordt er geschreven.
2. **Rekordbox moet dicht zijn.** Proces-check via `psutil` + `pgrep -x rekordbox` (de achtergrond-
   `rekordboxAgent` wordt genegeerd). Draait Rekordbox, dan wordt de actie geweigerd — er is dan niets
   geschreven en ook geen backup gemaakt. Vlak vóór het schrijven wordt nogmaals gecheckt.
3. **Eerst backup, dan schrijven.** Naar `backups/<YYYYMMDD-HHMMSS-µs>_<actie>/`:
   `master.db` (+ `-wal`/`-shm`), `masterPlaylists6.xml`, alle `*SETTING*.DAT` en de volledige
   ANLZ-map `share/PIONEER/USBANLZ`. Op macOS via APFS-clone (`cp -c`): direct klaar en kost pas
   ruimte als een bestand verandert. De kopie wordt geverifieerd (sha256 van master.db, aantal
   bestanden/bytes van ANLZ); mislukt dat, dan wordt er **niet** geschreven. Alle backups blijven
   bewaard (optioneel `RBMCP_BACKUP_KEEP=N`).
4. **Log.** Elke actie (ook dry-runs en mislukte pogingen) komt als JSON-regel in
   `logs/write-actions.log`: timestamp, actie, status (`DRY-RUN`/`OK`/`FAILED`), tracks, backup-pad, details.
5. **Herstel:** `restore_backup(backup_path, dry_run=false)` (maakt eerst nog een backup van de huidige staat).

**Rekordbox' eigen backup** (Bestand › Bibliotheek › Reservekopie maken) is alleen via de GUI te
starten: er is geen CLI-optie, URL-scheme of AppleScript-dictionary voor. De server kopieert de
bestanden daarom zelf. Rekordbox' eigen rotatie `master.backup*.db` wordt niet aangeraakt.

### pyrekordbox-valkuilen die hier zijn afgevangen
- BPM staat als integer ×100 in de DB (`13000` = 130.00) — alle tools werken met gewone BPM.
- Verwijderen gaat via `db.delete(row)` + **één** `db.commit()` aan het eind (geen `remove_content`).
- Dedupe kiest de keeper pas **na** `Path(FolderPath).exists()`: een dode DB-verwijzing wint nooit van
  een bestaand bestand. Playlist-, history- en MyTag-verwijzingen van verliezers gaan naar de keeper.
- Elk track-resultaat heeft `exists`; sets slaan tracks zonder lokaal bestand (iCloud e.d.) over.
- Soft-deleted rijen (`rb_local_deleted=1`) worden genegeerd.
- Staat een naam dubbel in de collectie, dan wordt er niet gegokt: de tool vraagt om het track-ID.

## Cloud Library Sync & Google Drive / Dropbox

Werkt ook met **Creative/Core + Cloud Option** (Cloud Library Sync) en muziek in Google Drive of
Dropbox. Begin altijd met `get_cloud_status`: die laat zien of sync actief is (en op basis van welk
bewijs in de database), welke verwijder-strategie gebruikt wordt, en hoeveel tracks lokaal staan,
alleen online staan (placeholder) of ontbreken.

- **Verwijderen = soft delete bij actieve sync.** Rekordbox synct via volgnummers (USN) en een
  verwijder-vlag (`rb_local_deleted`). Een harde `DELETE` ziet de sync niet, waardoor een verwijderde
  playlist/track kan terugkomen. Is sync actief, dan zetten `delete_playlist`,
  `remove_tracks_from_playlist`, `dedupe_library` en `write_cue_points(replace_existing)` die vlag
  (de USN wordt verhoogd, zodat Rekordbox het uploadt). Zonder sync: `db.delete()` + één commit.
  De gekozen modus staat in elke dry-run (`delete_mode`). Forceren kan met `RBMCP_DELETE_MODE=soft|hard`.
  De ingebouwde "Trial playlist - Cloud Library Sync" die elke installatie heeft, telt niet als bewijs.
- **Online-only bestanden** (Google Drive "streamen" / Dropbox "alleen online"): het bestand bestaat als
  placeholder, maar de audio staat niet lokaal. Zulke tracks krijgen wel het Rekordbox-genre, maar
  **geen ID3-tag** (dat zou een download forceren). Elk track-resultaat heeft `file_state`
  (`local` / `online_only` / `missing`) en `in_cloud_storage`; `search_tracks(only_local_files=true)`
  filtert ze weg. Bij dedupe wint een lokale kopie van een online-only kopie.
- **Dropbox** werkt hetzelfde als Google Drive: de huidige Dropbox-app (File Provider,
  `~/Library/CloudStorage/Dropbox`) wordt volledig herkend, inclusief online-only bestanden. Met de oude
  Dropbox Smart Sync (van vóór File Provider) wordt de map herkend, maar online-only alleen via een
  heuristiek (0 blokken op schijf).
- **Nooit uit de cloud-map verplaatsen.** `organize_library_by_genre(move_files=true)` en de
  dedupe-prullenbak raken bestanden onder `~/Library/CloudStorage/GoogleDrive-*`, `…/Dropbox*`,
  `~/Google Drive` of `~/Dropbox` niet aan (extra mappen: `RBMCP_CLOUD_ROOTS`, `:`-gescheiden).
- **Werkvolgorde met sync:** laat Rekordbox volledig syncen → sluit Rekordbox → MCP-acties →
  open Rekordbox **op deze Mac** zodat de wijzigingen geüpload worden → pas daarna op een ander
  apparaat werken. Schrijfacties tonen deze herinnering (`cloud_sync_note`) zolang sync actief is.
- **Voorbehoud:** het sync-protocol is niet gedocumenteerd. Test eerst: verwijder via de MCP een
  lege test-playlist, open Rekordbox, en kijk of hij ook op je andere apparaat verdwijnt.
- Backups dekken de database + ANLZ-bestanden; de audio in Google Drive zit er (zoals altijd) niet in.

## Tools

| Tool | Wat | Schrijft |
|---|---|---|
| `get_status` | paden, Rekordbox-proces, aantallen, laatste backup | – |
| `get_cloud_status` | Cloud Library Sync actief?, verwijder-strategie, lokale / online-only / ontbrekende bestanden | – |
| `list_playlists`, `get_playlist_tracks` | playlists/folders met pad (`Sets/2026/Vrijdag`) | – |
| `search_tracks`, `get_track` | zoeken op tekst, BPM, genre, key (Am/8A), energy, rating | – |
| `create_playlist` | playlist of folder, ontbrekende parent-folders worden gemaakt | ✔ |
| `delete_playlist` | playlist of folder verwijderen | ✔ |
| `add_tracks_to_playlist` | tracks (ID of "Artiest - Titel") toevoegen, op positie | ✔ |
| `remove_tracks_from_playlist`, `reorder_playlist` | verwijderen / herordenen (lijst of sort_by) | ✔ |
| `build_set_from_criteria` | set uit tracknamen of criteria, ordering `warmup_peak_cooldown`, `energy_ramp`, `harmonic` (Camelot), `bpm_ascending`, … | ✔ |
| `set_track_genre` | genre in Rekordbox + ID3/bestandstag (mutagen) | ✔ |
| `organize_library_by_genre` | genres synchroniseren (rekordbox→ID3, ID3→rekordbox, map→genre) en optioneel bestanden naar `02 Library/<Genre>/` verplaatsen | ✔ |
| `dedupe_library` | rapport, of `action="remove"`: samenvoegen in keeper, verliezer-bestand naar `duplicates_trash/` (nooit gewist) | ✔ |
| `get_settings`, `update_setting` | MySettings lezen (waarden + toegestane opties) / wijzigen met verificatie | ✔ |
| `inspect_track_cues`, `verify_cue_format` | cues uit DB, contentCue en ANLZ naast elkaar; mapping-check | – |
| `write_cue_points` | **experimenteel** – hot/memory cues + loops naar `djmdCue` | ✔ |
| `write_beatgrid` | **experimenteel** – constante-BPM-grid in ANLZ `.DAT`/`.EXT` + BPM-veld | ✔ |
| `backup_now`, `list_backups`, `restore_backup`, `get_action_log` | backups en logboek | (restore ✔) |

**Energy** komt uit het comment-veld (Mixed In Key-stijl, bijv. `8A - Energy 7`). Zonder energy
valt `energy_ramp` terug op de rating.

### Cues & beatgrids — lees dit eerst
- Cues worden als rijen in `djmdCue` geschreven (wat de Rekordbox-collectie toont). De ANLZ
  `PCOB`/`PCO2`-cue-tags worden **niet** herschreven (pyrekordbox heeft daar geen builder voor).
- De mapping hot cue A–H → `djmdCue.Kind` is niet gedocumenteerd; aangenomen is A,B,C,D,E,F,G,H →
  1,2,3,5,6,7,8,9. `write_cue_points` in `execute` weigert hot cues tenzij `verify_cue_format`
  die mapping in **jouw** bibliotheek bevestigt (vergelijkt DB-cues met ANLZ-cues van tracks die al
  hot cues hebben), en weigert als de track een `contentCue`-rij (RB 6.6+/7 JSON-cues) heeft —
  tenzij `force=true`.
- Aanpak: zet in Rekordbox handmatig een paar hot cues A–D op één track, sluit Rekordbox, draai
  `inspect_track_cues` + `verify_cue_format`, en probeer daarna pas `execute` op een testtrack.
- `write_beatgrid` houdt het aantal beats gelijk aan de huidige analyse, verifieert de gebouwde
  bytes vóór het wegschrijven, en zet bij een fout de originele ANLZ-bytes terug.

## Installatie (macOS)

```bash
cd ~/Music/DJ/05\ Tools
git clone https://github.com/rubendegoededeheij/rekordbox-mcp-v1.git rekordbox-mcp
~/Music/DJ/05\ Tools/venv/bin/pip install -r rekordbox-mcp/requirements.txt
```

Standaardpaden (overschrijfbaar met env-variabelen):

| Variabele | Standaard |
|---|---|
| `RBMCP_DB_PATH` | `~/Library/Pioneer/rekordbox/master.db` |
| `RBMCP_HOME` (backups/, logs/, duplicates_trash/) | de map van deze repo |
| `RBMCP_LIBRARY_ROOT` | `~/Music/DJ/02 Library` |
| `RBMCP_BACKUP_KEEP` | `0` = alles bewaren |
| `RBMCP_DB_KEY` | leeg (pyrekordbox kent de sleutel) |

## Registreren in Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (vervang `ruben` door je gebruikersnaam):

```json
{
  "mcpServers": {
    "rekordbox": {
      "command": "/Users/ruben/Music/DJ/05 Tools/venv/bin/python",
      "args": ["-m", "rekordbox_mcp"],
      "cwd": "/Users/ruben/Music/DJ/05 Tools/rekordbox-mcp",
      "env": {
        "PYTHONPATH": "/Users/ruben/Music/DJ/05 Tools/rekordbox-mcp",
        "RBMCP_LIBRARY_ROOT": "/Users/ruben/Music/DJ/02 Library"
      }
    }
  }
}
```

Herstart Claude Desktop; de tools verschijnen onder "rekordbox". Alternatief: `"command"` naar
`/Users/ruben/Music/DJ/05 Tools/rekordbox-mcp/run_server.sh` zonder args (gebruikt `../venv`).

**Claude Code:** `claude mcp add rekordbox -- "/Users/ruben/Music/DJ/05 Tools/rekordbox-mcp/run_server.sh"`

**Hermes (of een andere MCP-client):** registreer een stdio-server met hetzelfde commando en
dezelfde argumenten/env als hierboven, bijvoorbeeld in YAML:

```yaml
mcp_servers:
  rekordbox:
    command: "/Users/ruben/Music/DJ/05 Tools/rekordbox-mcp/run_server.sh"
    args: []
    env:
      RBMCP_LIBRARY_ROOT: "/Users/ruben/Music/DJ/02 Library"
```

## Los testen

```bash
cd ~/Music/DJ/05\ Tools/rekordbox-mcp

# 1) Zelf-test tegen je echte bibliotheek via echte MCP-stdio: alleen lezen, dry-runs en één backup
#    (toont ls -la van de backupmap). Er wordt niets in Rekordbox gewijzigd.
../venv/bin/python scripts/selftest.py
../venv/bin/python scripts/selftest.py --cue-track "Artiest - Titel"   # cue dry-run op een specifieke track

# 2) Hetzelfde tegen een wegwerp-testbibliotheek (pyrekordbox-testdata, echt versleuteld RB-formaat)
../venv/bin/python scripts/selftest.py --test-library /tmp/rb-test

# 3) Unit/integratietests (alle execute-paden, uitsluitend op wegwerpkopieën)
../venv/bin/pip install pytest && ../venv/bin/python -m pytest -q

# 4) Interactief in de MCP Inspector
npx @modelcontextprotocol/inspector ../venv/bin/python -m rekordbox_mcp
```

## Voorbeelden van opdrachten in de chat

- "Maak een folder *Sets/2026* met een playlist *Vrijdag Kade*."
- "Bouw een set van 90 minuten tech house tussen 124 en 128 BPM, warm-up → piek → cooldown, in *Sets/Vrijdag*." (eerst dry-run, dan bevestigen)
- "Zet het genre van deze 5 tracks op Melodic Techno, ook in de ID3-tags."
- "Laat dubbele tracks zien" → "ruim groep 3 en 7 op".
- "Zet quantize op on." / "Wat staat er in mijn MySettings?"

## Credits
- [pyrekordbox](https://github.com/dylanljones/pyrekordbox) (MIT) — database/ANLZ/MySetting-formaten;
  de testdata in `tests/fixtures/pyrekordbox_testdata` komt uit dat project.
- [davehenke/rekordbox-mcp](https://github.com/davehenke/rekordbox-mcp) (MIT) — bekeken als referentie.
