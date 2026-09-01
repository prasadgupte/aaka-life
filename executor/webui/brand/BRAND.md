# Aaka — Brand (implementation)

Strategic spec lives in `aaka-repo/docs/gtm/06_brand.md`. This file is the **distilled implementation guide**: what to do in code, not the inspiration history.

---

## Essence

Aaka is **warm, competent, and quietly confident**. A product for the people you share your life with. It respects the intelligence of the operator and the simplicity needed by everyone else who uses it.

**Three words:** Warm. Grounded. Capable.

---

## Voice

| Quality   | Means                                          | Doesn't mean                       |
|-----------|------------------------------------------------|------------------------------------|
| Warm      | Friendly, conversational, human                | Cutesy, over-familiar, emoji-heavy |
| Capable   | Specific, technical when needed, confident     | Jargon, condescending, verbose     |
| Grounded  | Honest, practical, unpretentious               | Boring, plain, unambitious         |
| Inclusive | Universal, no assumptions, welcoming           | Performative, preachy, qualified   |

### Word palette

| Use                       | Avoid                            |
|---------------------------|----------------------------------|
| runs in your home         | deployed to the cloud            |
| zero tokens               | AI-powered                       |
| they already use WhatsApp | install our app                  |
| local-first               | privacy-focused (overused)       |
| home / away               | executor / sensor (internal)     |
| set up once               | configure                        |
| just text                 | interact with                    |

---

## The mark + wordmark

| Element  | Form                                          | Use                                  |
|----------|-----------------------------------------------|--------------------------------------|
| Mark     | Fraunces 800 **italic** `&` in Teal           | Favicon, watermark, standalone       |
| Wordmark | `aaka.` in Fraunces 700 (lowercase + period)  | The name. Always lowercase.          |
| Lockup   | `& aaka.` — mark left of wordmark             | Nav, hero. Mark ~1.25× cap-height.   |

The period is intentional — a declarative full stop. Domain: `aaka.life`.

**Never embed the `&` inside the word.** `a&ka` reads as `aeka` because Fraunces italic renders `&` as the calligraphic *et* ligature. Mark and wordmark are always separate elements.

---

## Palette (canonical hexes)

### Accents

| Name      | Hex       | Role                                              |
|-----------|-----------|---------------------------------------------------|
| Teal      | `#00B4A2` | Primary. CTAs, links, success, the & mark.        |
| Berry     | `#FF6B8A` | Warmth. Alerts, emphasis, errors.                 |
| Sunshine  | `#FFBA49` | Attention. Warnings, pending states.              |
| Iris      | `#7B68EE` | Technical. Labels, code accents, privacy.         |

### Light mode (Coral-Fresh)

| Name   | Hex       | Role                          |
|--------|-----------|-------------------------------|
| Coral  | `#FFF5F0` | Default canvas.               |
| White  | `#FFFFFF` | Cards and surfaces.           |
| Peach  | `#FFEDE5` | Secondary backgrounds.        |
| Ink    | `#1E2030` | Headlines, primary text.      |
| Slate  | `#556170` | Body text.                    |
| Sand   | `#F0DDD4` | Borders, dividers.            |

### Dark mode (Dimmed Aurora)

| Name      | Hex       | Role                          |
|-----------|-----------|-------------------------------|
| Night     | `#161520` | Default canvas.               |
| Dusk      | `#1E1D2C` | Cards and surfaces.           |
| Elevated  | `#242338` | Raised elements.              |
| Light     | `#F0F0F0` | Headlines.                    |
| Fog       | `#8890A0` | Body text.                    |
| Border    | `#2A2940` | Borders, dividers.            |

### Signature gradient

```
linear-gradient(90deg, #00B4A2, #7B68EE, #FF6B8A, #FFBA49)
```

The only place all four accents meet. Used on the **top border of major cards** (4–5px), section dividers, and loading bars. Never on text.

---

## Typography

| Role    | Font              | Weight  |
|---------|-------------------|---------|
| Display | Fraunces          | 600–800 |
| Body    | Plus Jakarta Sans | 300–500 |
| Mono    | JetBrains Mono    | 400–500 |

| Level   | Size    | Font                        |
|---------|---------|-----------------------------|
| Display | 48–64px | Fraunces 700                |
| H1      | 36–40px | Fraunces 700                |
| H2      | 24px    | Fraunces 600                |
| H3      | 20px    | Fraunces 600                |
| Body    | 16px    | Plus Jakarta Sans 400       |
| Small   | 14px    | Plus Jakarta Sans 400       |
| Label   | 12px    | JetBrains Mono 500 (caps)   |
| Code    | 14px    | JetBrains Mono 400          |

---

## Shape

- **Corners:** 12–16px on cards (`--aaka-radius`). 20px+ on pills and buttons (`--aaka-radius-lg`).
- **No hard geometry.** No triangles, no hexagons. Everything is soft.
- **Organic shapes** at 5–8% opacity for decoration.
- **Pill tags:** solid in light, ghost in dark.
- **Glass cards:** translucent surfaces with gradient edge-light in dark mode.

---

## Do / Don't (binding for this repo)

### Do
- Use the lockup `& aaka.` — mark separate from wordmark
- Use Coral (`#FFF5F0`) as default light background, not cold white
- Dimmed Aurora in dark mode (radial gradients at low opacity)
- Pill tags for feature categories
- Gradient bar on every major card/section
- `&` watermark in hero / splash areas (3–5% opacity)
- Fraunces for headlines, Plus Jakarta Sans for body, JetBrains Mono for labels/code
- Rounded corners (12–16 on containers, 20+ on pills)

### Don't
- More than 2 accent colours per composition
- Cold white (`#FFFFFF`) as a page background
- Gradient on text (bars and splashes only)
- Embed `&` inside the word (`a&ka` reads as "aeka")
- Mark upright — it is always italic
- Sharp corners or hard geometric shapes
- Lock or shield metaphors — privacy is calm, not fear
- Gendered language or nuclear-family assumptions
- Dark aurora at full intensity — always dimmed
