# 📖 BijbelAPI

**Dé #1 Nederlandse Bijbel API voor ontwikkelaars.**

*Een REST API om Bijbelteksten en Nederlandstalige commentaargegevens op te vragen over meerdere ondersteunde vertalingen. Bedoeld voor ontwikkelaars, theologen, studenten, kerken en digitale Bijbelprojecten.*

[Bekijk Bijbel API](https://www.bijbelapi.com)

## Functies

- 🔀 **Willekeurige verzen** ophalen  
- 🔍 **Zoeken in bijbeltekst** over de geladen vertalingen  
- 📚 **Structuuroverzicht** van boeken, hoofdstukken en verzen  
- 📖 **Specifieke verzen of passages** opvragen  
- 📅 **Dagtekst** (optioneel met vaste seed voor herhaling)
- 🧠 **Slim parsen** van complexe bijbelverwijzingen  
- 📝 **Commentaar-endpoint** voor hoofdstukken en verzen (Nederlandstalige bron) 

## 🌐 API-endpoints


| Methode  | Endpoint                                                    | Beschrijving                                                |
| -------- | ----------------------------------------------------------- | ----------------------------------------------------------- |
| GET      | `/`                                                         | Startpagina met uitleg en link naar documentatie            |
| GET      | `/health`                                                   | Health check                                                |
| GET      | `/api/random`                                               | Willekeurig vers                                            |
| GET      | `/api/verse?book=...&chapter=...&verse=...`                 | Specifiek vers                                              |
| GET      | `/api/passage?book=...&chapter=...&start=...&end=...`       | Meerdere verzen (bereik)                                    |
| GET      | `/api/books`                                                | Alle boeken                                                 |
| GET      | `/api/chapters?book=...`                                    | Hoofdstukken in een boek                                    |
| GET      | `/api/verses?book=...&chapter=...`                          | Versnummers in een hoofdstuk                                |
| GET      | `/api/search?query=...`                                     | Zoeken in bijbeltekst                                       |
| GET      | `/api/daytext?seed=...`                                     | Dagtekst, optioneel met seed                                |
| GET      | `/api/versions`                                             | Beschikbare vertalingen + metadata                          |
| GET      | `/api/chapter?book=...&chapter=...`                         | Hele hoofdstuk                                              |
| GET      | `/api/commentary?source=...&book=...&chapter=...`           | Commentaar op een heel hoofdstuk (bijv. `matthew_henry_nl`) |
| GET      | `/api/commentary?source=...&book=...&chapter=...&verse=...` | Commentaar op één vers                                      |
| **POST** | `**/api/parse/reference`**                                  | **Complexe verwijzing parseren (JSON-body)**                |
| **GET**  | `**/api/parse/reference/{ref}`**                            | **Verwijzing via URL parseren**                             |
| **POST** | `**/api/parse/references`**                                 | **Meerdere verwijzingen tegelijk**                          |


De **publieke BijbelAPI** staat in:

- `/docs` – Swagger UI  
- `/redoc` – ReDoc

## Slim verwijzingen parsen

De parse-endpoints ondersteunen complexe bijbelverwijzingen die traditionele API’s vaak niet goed aankunnen:

### Ondersteunde verwijzingstypen


| Type                            | Voorbeeld              | Uitleg                                      |
| ------------------------------- | ---------------------- | ------------------------------------------- |
| **Niet-aaneengesloten reeksen** | `Psalm 104:26-36,37`   | Meerdere versbereiken                       |
| **Over hoofdstukken heen**      | `Johannes 3:16-4:1`    | Van het ene hoofdstuk naar het andere       |
| **Alleen hoofdstuk**            | `Filemon 1-21`         | Hele hoofdstuk als bereik                   |
| **Optionele verzen**            | `Lukas 1:39-45[46-55]` | Hoofdtekst plus optioneel blok tussen `[ ]` |
| **Lettersuffix bij vers**       | `Habakuk 3:2-19a`      | Verzen met lettersuffix                     |
| **“Tot einde hoofdstuk”**       | `Jeremia 18:5-end`     | Van dit vers tot het eind van het hoofdstuk |


### Voorbeelden

```bash
# Eén complexe verwijzing (GET)
curl "https://bijbelapi.com/api/parse/reference/Psalm%20104:26-36,37?version=bb"

# Parse via POST met gekozen vertaling
curl -X POST "https://bijbelapi.com/api/parse/reference" \
  -H "Content-Type: application/json" \
  -d '{"reference": "Lukas 1:39-45[46-55]", "version": "hsv"}'

# Meerdere verwijzingen
curl -X POST "https://bijbelapi.com/api/parse/references" \
  -H "Content-Type: application/json" \
  -d '{"references": ["Psalm 104:26-36,37", "Jeremia 18:1-11"], "version": "sv"}'
```

### Responsvoorbeeld

```json
{
  "reference": "Psalm 104:26-36,37",
  "parsed": true,
  "book": "Psalms",
  "chapter": "104",
  "verses": [
    {"verse": "26", "text": "…"},
    {"verse": "27", "text": "…"}
  ],
  "formatted_text": "26 … 27 …"
}
```

## Ondersteunde vertalingen

Huidige vertaalcodes:

- `sv` (Statenvertaling)
- `hsv` (Herziene Statenvertaling)
- `bb` (BasisBijbel)

- `matthew-henry` (Bijbelcommentaar)

## Verdere uitbreiding

Geplande richtingen:

- Meer Nederlandse en meertalige bijbeldatasets toevoegen.
- Rijkere limieten en gebruiksregeling per API-sleutel.
- Technische en productdocumentatie verder verbeteren.
- Meer tooling rond databeheer, transformatie en validatie.

## Licentie

Deze API staat onder de [MIT-licentie](LICENSE).

## Contact

- GitHub: [@AlexLamper](https://github.com/AlexLamper)
- Mail: `info@bijbelquiz.com`
- BijbelAPI: [https://bijbelapi.com](https://bijbelapi.com)

## 📌 Versie

**Huidige versie:** `v1.2`
