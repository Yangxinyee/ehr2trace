"""How a hospital writes a drug name, and how RxNorm writes the same thing.

An EHR medication name is not free text. `CEFAZOLIN 2 GRAM/100 ML IN 0.9% SODIUM
CHLORIDE INTRAVENOUS PIGGYBACK` is an ingredient, a strength, a diluent and a dose
form, in that order, and RxNorm's `Cefazolin 2000 MG Intravenous Solution` is the same
four things in RxNorm's spelling. Matching them is arithmetic and vocabulary, not text
similarity -- which is why embedding the whole string ranks the right concept outside
the top 32 while the wrong package size ranks first.

Nothing here is a concept id. Every RxNorm name in this file is resolved against the
loaded vocabulary at run time, and a name this file gets wrong shows up as a term that
did not match rather than as a term that matched something else.

The tables are ordered *tiers*, not sets. `INJECTION SOLUTION` is `Injectable
Solution` in RxNorm and plain `Injection` in RxNorm Extension, and the same physical
product exists under both spellings; a source string is tried against the first tier
alone and only falls back to the next when that finds nothing. Widening never makes a
match less specific -- it only ever reaches a spelling of the same form -- and the
uniqueness rule still applies at every tier, so a widening that pulls in two different
drugs abstains instead of guessing.
"""

from __future__ import annotations

#: Source dose-form phrase -> tiers of RxNorm dose form names to try, in order.
DOSE_FORMS: dict[str, tuple[tuple[str, ...], ...]] = {
    # -- oral solids ------------------------------------------------------
    "TABLET": (("Oral Tablet",), ("Chewable Tablet", "Disintegrating Oral Tablet")),
    "ORAL TABLET": (("Oral Tablet",),),
    "HALF-TAB": (("Oral Tablet",),),
    "TABLET,DELAYED RELEASE": (("Delayed Release Oral Tablet",),),
    "DELAYED RELEASE TABLET": (("Delayed Release Oral Tablet",),),
    "TABLET,EXTENDED RELEASE": (("Extended Release Oral Tablet",),
                                ("24 Hour Extended Release Tablet",
                                 "12 hour Extended Release Tablet")),
    "EXTENDED RELEASE TABLET": (("Extended Release Oral Tablet",),
                                ("24 Hour Extended Release Tablet",)),
    "TABLET,EXTENDED RELEASE 24 HR": (("24 Hour Extended Release Tablet",),
                                      ("Extended Release Oral Tablet",)),
    "TABLET,EXTENDED RELEASE 12 HR": (("12 hour Extended Release Tablet",),
                                      ("Extended Release Oral Tablet",)),
    "TABLET,DISPERSIBLE": (("Disintegrating Oral Tablet",),),
    "TABLET,DISINTEGRATING": (("Disintegrating Oral Tablet",),),
    "DISINTEGRATING TABLET": (("Disintegrating Oral Tablet",),),
    "CHEWABLE TABLET": (("Chewable Tablet",),),
    "TABLET,CHEWABLE": (("Chewable Tablet",),),
    "SUBLINGUAL TABLET": (("Sublingual Tablet",),),
    "TABLET,SUBLINGUAL": (("Sublingual Tablet",),),
    "BUCCAL TABLET": (("Buccal Tablet",),),
    "EFFERVESCENT TABLET": (("Effervescent Oral Tablet",),),
    "VAGINAL TABLET": (("Vaginal Tablet",),),
    "CAPSULE": (("Oral Capsule",),),
    "ORAL CAPSULE": (("Oral Capsule",),),
    "CAPSULE,DELAYED RELEASE": (("Delayed Release Oral Capsule",),),
    "DELAYED RELEASE CAPSULE": (("Delayed Release Oral Capsule",),),
    "CAPSULE,EXTENDED RELEASE": (("Extended Release Oral Capsule",),
                                 ("24 Hour Extended Release Capsule",
                                  "12 hour Extended Release Capsule")),
    "EXTENDED RELEASE CAPSULE": (("Extended Release Oral Capsule",),),
    "CAPSULE,EXTENDED RELEASE 24 HR": (("24 Hour Extended Release Capsule",),
                                       ("Extended Release Oral Capsule",)),
    "CAPSULE,SPRINKLE": (("Oral Capsule",),),
    # a dose pack or starter pack is a count of ordinary tablets; the count is noise
    "TABLETS IN A DOSE PACK": (("Oral Tablet",),),
    "TABLET IN A DOSE PACK": (("Oral Tablet",),),
    "TABLETS IN A STARTER PACK": (("Oral Tablet",),),
    "CAPSULES IN A DOSE PACK": (("Oral Capsule",),),

    # -- injectables ------------------------------------------------------
    # RxNorm says "Injectable Solution"; RxNorm Extension says "Injection" for the
    # same vial. Both tiers are tried before a term is given up on.
    "INJECTION SOLUTION": (("Injection", "Injectable Solution"), ("Intravenous Solution",)),
    "SOLUTION FOR INJECTION": (("Injection", "Injectable Solution"), ("Intravenous Solution",)),
    "INJECTABLE SOLUTION": (("Injection", "Injectable Solution"),),
    "INJ SOLN": (("Injection", "Injectable Solution"), ("Intravenous Solution",)),
    "SOLN FOR INJ": (("Injection", "Injectable Solution"), ("Intravenous Solution",)),
    "INJECTION": (("Injection",), ("Injectable Solution", "Intravenous Solution")),
    "INTRA-CATHETER SOLUTION": (("Injectable Solution",), ("Injection",)),
    "INTRACATHETER SOLUTION": (("Injectable Solution",), ("Injection",)),
    "INJECTABLE EMULSION": (("Injectable Solution",), ("Injection",)),
    "INJECTION EMULSION": (("Injectable Solution",), ("Injection",)),
    "INTRAVENOUS EMULSION": (("Intravenous Solution",), ("Injection", "Injectable Solution")),
    "SUSPENSION FOR INJECTION": (("Injectable Suspension",), ("Injection",)),
    "INJECTION SUSPENSION": (("Injectable Suspension",), ("Injection",)),
    "SUBCUTANEOUS SOLUTION": (("Injectable Solution",), ("Injection",)),
    "SUBCUTANEOUS SOLN": (("Injectable Solution",), ("Injection",)),
    "SUBCUTANEOUS INJECTION": (("Injectable Solution",), ("Injection",)),
    "INTRAMUSCULAR SOLUTION": (("Intramuscular Solution",), ("Injection", "Injectable Solution")),
    "SYRINGE": (("Prefilled Syringe",), ("Injection", "Injectable Solution")),
    "INJECTION SYRINGE": (("Prefilled Syringe",), ("Injection", "Injectable Solution")),
    "PREFILLED SYRINGE": (("Prefilled Syringe",), ("Injection", "Injectable Solution")),
    "SUBCUTANEOUS SYRINGE": (("Prefilled Syringe",), ("Injection", "Injectable Solution")),
    "INTRAVENOUS SYRINGE": (("Prefilled Syringe",), ("Injection", "Injectable Solution")),
    "IM SYRINGE": (("Prefilled Syringe",), ("Intramuscular Solution", "Injection")),
    "INTRAMUSCULAR SYRINGE": (("Prefilled Syringe",), ("Intramuscular Solution", "Injection")),
    "PEN": (("Pen Injector",), ("Injection", "Injectable Solution")),
    "SUBCUTANEOUS PEN": (("Pen Injector",), ("Injection", "Injectable Solution")),
    "PEN INJECTOR": (("Pen Injector",), ("Injection", "Injectable Solution")),
    "AUTO-INJECTOR": (("Auto-Injector",), ("Prefilled Syringe", "Injection")),
    "CARTRIDGE": (("Cartridge",), ("Injection", "Injectable Solution")),
    "INTRAVENOUS CARTRIDGE": (("Cartridge",), ("Injection", "Injectable Solution")),
    "VIAL": (("Injectable Solution",), ("Injection",)),
    "AMPULE": (("Injectable Solution",), ("Injection",)),
    "AMPUL": (("Injectable Solution",), ("Injection",)),

    # -- infusions --------------------------------------------------------
    "INTRAVENOUS SOLUTION": (("Intravenous Solution",), ("Injection", "Injectable Solution")),
    "IV SOLUTION": (("Intravenous Solution",), ("Injection", "Injectable Solution")),
    "IV SOLN": (("Intravenous Solution",), ("Injection", "Injectable Solution")),
    "IV BOLUS": (("Intravenous Solution",), ("Injection", "Injectable Solution")),
    "INTRAVENOUS PIGGYBACK": (("Intravenous Solution",), ("Injection", "Injectable Solution")),
    "IV SOLUTION PREMIX": (("Intravenous Solution",), ("Injection", "Injectable Solution")),
    "INTRAVENOUS PREMIX": (("Intravenous Solution",), ("Injection", "Injectable Solution")),
    # A drug given into a vein that the vocabulary has only as a syringe or cartridge
    # at that strength (a PCA syringe of hydromorphone) is still that drug and strength.
    "INTRAVENOUS": (("Intravenous Solution",), ("Injection", "Injectable Solution"),
                    ("Prefilled Syringe", "Cartridge")),
    "IV": (("Intravenous Solution",), ("Injection", "Injectable Solution"),
           ("Prefilled Syringe", "Cartridge")),
    "SUBCUTANEOUS": (("Injectable Solution",), ("Injection",)),

    # -- oral liquids and powders -----------------------------------------
    "ORAL SOLUTION": (("Oral Solution",),),
    "ORAL LIQUID": (("Oral Solution",),),
    "ORAL ELIXIR": (("Oral Solution",),),
    "ORAL SYRUP": (("Oral Solution",),),
    "ORAL CONCENTRATE": (("Oral Solution",),),
    "ORAL SYRINGE": (("Oral Solution",), ("Oral Suspension",)),
    "SOLUTION": (("Oral Solution",),),
    "ORAL SUSPENSION": (("Oral Suspension",),),
    "ORAL SUSP": (("Oral Suspension",),),
    "SUSPENSION": (("Oral Suspension",),),
    "ORAL POWDER": (("Oral Powder",), ("Powder for Oral Solution",)),
    "ORAL POWDER PACKET": (("Oral Powder",), ("Powder for Oral Solution",)),
    "ORAL PACKET": (("Oral Powder",), ("Powder for Oral Solution", "Oral Granules")),
    "POWDER PACKET": (("Oral Powder",), ("Powder for Oral Solution",)),
    "PACKET": (("Oral Powder",), ("Oral Granules",)),
    "POWDER FOR ORAL SOLUTION": (("Powder for Oral Solution",), ("Oral Powder",)),
    "POWDER FOR ORAL SUSPENSION": (("Powder for Oral Suspension",), ("Oral Powder",)),
    "ORAL GRANULES": (("Oral Granules",),),
    "GRANULES": (("Oral Granules",),),
    "ORAL GEL": (("Oral Gel",),),
    "ORAL PASTE": (("Oral Paste",),),
    "ORAL LOZENGE": (("Oral Lozenge",),),
    "LOZENGE": (("Oral Lozenge",),),
    "TROCHE": (("Oral Lozenge",),),
    "CHEWING GUM": (("Chewing Gum",),),
    "GUM": (("Chewing Gum",),),
    "ORAL FILM": (("Oral Film",),),
    "ORAL STRIP": (("Oral Strip",),),
    "SUBLINGUAL FILM": (("Sublingual Film",),),
    "BUCCAL FILM": (("Buccal Film",),),

    # -- inhaled ----------------------------------------------------------
    "SOLUTION FOR NEBULIZATION": (("Inhalation Solution",),),
    "NEBULIZER SOLUTION": (("Inhalation Solution",),),
    "NEBULIZATION SOLUTION": (("Inhalation Solution",),),
    "NEBULIZATION SOLN": (("Inhalation Solution",),),
    "SOLUTION FOR INHALATION": (("Inhalation Solution",),),
    "INHALATION SOLUTION": (("Inhalation Solution",),),
    "INHALATION SOLN": (("Inhalation Solution",),),
    "AEROSOL INHALER": (("Metered Dose Inhaler",), ("Inhalation Spray", "Dry Powder Inhaler")),
    "METERED DOSE INHALER": (("Metered Dose Inhaler",),),
    "INHALATION AEROSOL": (("Metered Dose Inhaler",), ("Inhalation Spray",)),
    "AEROSOL": (("Metered Dose Inhaler",), ("Inhalation Spray",)),
    "INHALER": (("Metered Dose Inhaler",), ("Dry Powder Inhaler",)),
    "DISKUS": (("Dry Powder Inhaler",),),
    "RESPIMAT": (("Metered Dose Inhaler",), ("Inhalation Spray",)),
    "DRY POWDER INHALER": (("Dry Powder Inhaler",),),
    "INHALATION POWDER": (("Inhalation Powder",), ("Dry Powder Inhaler",)),

    # -- nasal, eye, ear ---------------------------------------------------
    "NASAL SPRAY": (("Nasal Spray",), ("Metered Dose Nasal Spray", "Nasal Solution")),
    "NASAL SPRAY,SUSPENSION": (("Nasal Spray",), ("Metered Dose Nasal Spray", "Nasal Suspension")),
    "NASAL SPRAY,SOLUTION": (("Nasal Spray",), ("Metered Dose Nasal Spray", "Nasal Solution")),
    "NASAL SOLUTION": (("Nasal Solution",), ("Nasal Spray",)),
    "NASAL INHALER": (("Nasal Inhaler",), ("Metered Dose Nasal Spray",)),
    "EYE DROPS": (("Ophthalmic Solution",), ("Ophthalmic Suspension",)),
    "EYE DROPS,SOLUTION": (("Ophthalmic Solution",),),
    "EYE DROPS,SUSPENSION": (("Ophthalmic Suspension",), ("Ophthalmic Solution",)),
    "OPHTHALMIC SOLUTION": (("Ophthalmic Solution",),),
    "OPHTHALMIC SUSPENSION": (("Ophthalmic Suspension",),),
    "OPHTHALMIC DROPS": (("Ophthalmic Solution",), ("Ophthalmic Suspension",)),
    "EYE OINTMENT": (("Ophthalmic Ointment",),),
    "OPHTHALMIC OINTMENT": (("Ophthalmic Ointment",),),
    "EYE GEL": (("Ophthalmic Gel",),),
    "EAR DROPS": (("Otic Solution",), ("Otic Suspension",)),
    "EAR DROPS,SUSPENSION": (("Otic Suspension",), ("Otic Solution",)),
    "OTIC SOLUTION": (("Otic Solution",),),
    "OTIC SUSPENSION": (("Otic Suspension",),),

    # -- topical, transdermal, rectal, vaginal -----------------------------
    "TOPICAL CREAM": (("Topical Cream",),),
    "CREAM": (("Topical Cream",),),
    "TOPICAL OINTMENT": (("Topical Ointment",),),
    "OINTMENT": (("Topical Ointment",),),
    "TOPICAL GEL": (("Topical Gel",),),
    "GEL": (("Topical Gel",),),
    "TOPICAL LOTION": (("Topical Lotion",),),
    "LOTION": (("Topical Lotion",),),
    "TOPICAL SOLUTION": (("Topical Solution",),),
    "TOPICAL SPRAY": (("Topical Spray",),),
    "TOPICAL AEROSOL": (("Topical Spray",),),
    "TOPICAL FOAM": (("Topical Foam",),),
    "TOPICAL POWDER": (("Topical Powder",),),
    "TOPICAL PATCH": (("Medicated Patch",), ("Transdermal System",)),
    "MEDICATED PATCH": (("Medicated Patch",),),
    "PATCH": (("Medicated Patch",), ("Transdermal System",)),
    "TRANSDERMAL PATCH": (("Transdermal System",), ("Medicated Patch",)),
    "TRANSDERMAL SYSTEM": (("Transdermal System",),),
    "TRANSDERMAL PATCH 24 HR": (("24 Hour Transdermal Patch",), ("Transdermal System",)),
    "TRANSDERMAL PATCH 72 HR": (("72 Hour Transdermal Patch",), ("Transdermal System",)),
    "MEDICATED PAD": (("Medicated Pad",),),
    "RECTAL SUPPOSITORY": (("Rectal Suppository",),),
    "SUPPOSITORY": (("Rectal Suppository",),),
    "VAGINAL SUPPOSITORY": (("Vaginal Suppository",),),
    "RECTAL SOLUTION": (("Rectal Solution",), ("Enema",)),
    "RECTAL ENEMA": (("Enema",), ("Rectal Solution",)),
    "ENEMA": (("Enema",), ("Rectal Solution",)),
    "RECTAL CREAM": (("Rectal Cream",),),
    "RECTAL GEL": (("Rectal Gel",),),
    "RECTAL FOAM": (("Rectal Foam",),),
    "VAGINAL CREAM": (("Vaginal Cream",),),
    "VAGINAL GEL": (("Vaginal Gel",),),
    "VAGINAL RING": (("Vaginal Ring",),),
    "VAGINAL INSERT": (("Vaginal Insert",),),

    # -- other -------------------------------------------------------------
    "MOUTHWASH": (("Mouthwash",),),
    "MOUTH WASH": (("Mouthwash",),),
    "ORAL RINSE": (("Mouthwash",),),
    "MUCOSAL SOLUTION": (("Mucous Membrane Topical Solution",),),
    "IRRIGATION SOLUTION": (("Irrigation Solution",),),
    "IRRIGATION": (("Irrigation Solution",),),
    "SHAMPOO": (("Medicated Shampoo",),),
    "SOAP": (("Medicated Bar Soap",), ("Medicated Liquid Soap",)),
    "TOOTHPASTE": (("Toothpaste",),),
    "OIL": (("Oil",),),
    "PASTE": (("Paste",),),
    "STICK": (("Stick",),),
    "IMPLANT": (("Drug Implant",),),
    "DRUG IMPLANT": (("Drug Implant",),),
    "PACK": (("Pack",),),
    "KIT": (("Pack",),),
}

#: Source unit token -> the UCUM code the vocabulary stores it under.
UNITS: dict[str, str] = {
    "MG": "mg", "MGS": "mg", "MILLIGRAM": "mg", "MILLIGRAMS": "mg",
    "G": "g", "GM": "g", "GRAM": "g", "GRAMS": "g",
    "MCG": "ug", "UG": "ug", "MICROGRAM": "ug", "MICROGRAMS": "ug",
    "NG": "ng", "NANOGRAM": "ng",
    "ML": "mL", "MILLILITER": "mL", "MILLILITERS": "mL", "CC": "mL",
    "L": "L", "LITER": "L",
    # RxNorm files insulin and heparin under UCUM `[U]` far more often than `[iU]`,
    # and treats the two as the same quantity; the families below join them.
    "UNIT": "[U]", "UNITS": "[U]", "IU": "[iU]", "UNT": "[U]",
    # low-molecular-weight heparins are dosed in anti-factor Xa units, which RxNorm
    # files as plain units
    "ANTI-XA UNIT": "[U]", "ANTI-XA UNITS": "[U]", "ANTI-XA": "[U]",
    "MEQ": "10*-3.eq", "MILLIEQUIVALENT": "10*-3.eq",
    "MMOL": "mmol", "MCI": "mCi", "MILLICURIE": "mCi",
    "%": "%",
    "ACTUATION": "{actuat}", "ACTUAT": "{actuat}", "PUFF": "{actuat}",
    "HR": "h", "HOUR": "h", "HOURS": "h",
}

#: UCUM code -> (family, factor to the family's base unit).
#:
#: Conversion happens only inside a family. A milligram is never compared with an
#: international unit, because for insulin or heparin they are not the same quantity
#: and no factor relates them. A unit absent from this table has no comparable form,
#: so a strength written in it does not match anything rather than matching loosely.
#:
#: Denominators are not all volumes. An inhaler is dosed per actuation and a patch per
#: hour, and the vocabulary says so -- `DRUG_STRENGTH` has 18,371 rows denominated in
#: `{actuat}` and 13,849 in `h`. Recognising only millilitres left every inhaler in this
#: export unmapped while `albuterol 0.09 MG/ACTUAT Metered Dose Inhaler` sat in the
#: vocabulary.
UNIT_FAMILIES: dict[str, tuple[str, float]] = {
    "mg": ("mass", 1.0), "g": ("mass", 1000.0), "ug": ("mass", 0.001),
    "ng": ("mass", 1e-6), "kg": ("mass", 1e6),
    "mL": ("volume", 1.0), "L": ("volume", 1000.0),
    "[U]": ("unit", 1.0), "[iU]": ("unit", 1.0),
    "[USP'U]": ("usp_unit", 1.0),
    "10*-3.eq": ("milliequivalent", 1.0),
    "mmol": ("millimole", 1.0), "mol": ("millimole", 1000.0),
    "%": ("percent", 1.0),
    "{actuat}": ("actuation", 1.0),
    "mCi": ("millicurie", 1.0),
    "h": ("hour", 1.0), "min": ("hour", 1 / 60),
    "cm2": ("area", 1.0),
}

#: Salt and hydrate suffixes. A source name may carry one where RxNorm's ingredient
#: does not (`OXYCODONE HCL` against `oxycodone`), so the full name is tried first and
#: the suffix dropped only if that fails -- `POTASSIUM CHLORIDE` must not become
#: `potassium`.
SALT_SUFFIXES: tuple[str, ...] = (
    "HYDROCHLORIDE", "HCL", "SULFATE", "SUCCINATE", "TARTRATE", "BITARTRATE",
    "MALEATE", "MESYLATE", "BESYLATE", "FUMARATE", "CITRATE", "ACETATE",
    "PHOSPHATE", "GLUCONATE", "LACTATE", "CARBONATE", "NITRATE", "BROMIDE",
    "TROMETHAMINE", "DISODIUM", "HYDROBROMIDE", "PAMOATE", "VALERATE",
    "PROPIONATE", "DIPROPIONATE", "FUROATE", "XINAFOATE", "ACETONIDE",
    "MONOHYDRATE", "ANHYDROUS", "HYCLATE", "MONONITRATE", "DINITRATE",
    "AXETIL", "ESTOLATE", "PALMITATE", "EDISYLATE", "LACTOBIONATE",
    "TOSYLATE", "OXALATE", "SALICYLATE", "STEARATE", "BENZOATE", "BORATE",
)

#: Words that describe how a drug is given rather than what it is. These are pharmacy
#: English, true of any US EHR: an IV piggyback is a route, `HFA` is a propellant, and
#: neither changes which concept the name denotes.
#:
#: Site-local words -- ward abbreviations, order-entry system names, order-set labels --
#: are deliberately *not* here. They differ at every hospital, so they are declared in
#: the dataset contract (`terminology.drug_name_noise`) instead of accumulating in the
#: core, which is the same rule that keeps column names out of it.
NOISE_WORDS: tuple[str, ...] = (
    "IVPB", "IVP", "IV PUSH", "CHEMO", "INFUSION", "PREMIX", "PREPACK", "PCA",
    "BOLUS FROM BAG", "CASSETTE", "COMPOUNDED", "NS", "D5W", "D10W", "D5NS", "D50W", "LR",
    "VARIABLE DOSE", "CUSTOM DOSE", "RANGE DOSE", "DEFAULT", "TOTAL VOLUME",
    "FOR INPATIENTS", "FOR ADULTS", "PER UNIT", "HALF-TAB", "STANDARD", "HFA", "MDI", "PF",
)

#: Words that say the drug went into a vein. None of them names a dose form, but each
#: rules out every form that is not an injectable: `HEPARIN BOLUS FROM BAG 100 UNIT/ML`
#: is not the irrigation solution of the same strength, and `PROPOFOL INFUSION 10
#: MG/ML` is not the oral suspension. A name carrying one of these and no dose-form
#: phrase is read as `INTRAVENOUS`, whose tiers are the injectable spellings. Without
#: it, a concentration that fits several forms is left for a person to settle.
IV_ROUTE_MARKERS: tuple[str, ...] = (
    "IVPB", "IVP", "IV PUSH", "IV BOLUS", "BOLUS FROM BAG", "INFUSION", "PREMIX",
    "PIGGYBACK", "PCA", "IV",
)

#: Abbreviations a pharmacy writes inside an ingredient name. Expanded before the name
#: is looked up, because `HYDROCORTISONE SOD SUCCINATE` is a spelling of an ingredient
#: the vocabulary has and not an ingredient the vocabulary lacks.
INGREDIENT_ABBREVIATIONS: dict[str, str] = {
    "SOD": "SODIUM", "SODIUM": "SODIUM", "SUCC": "SUCCINATE", "MAG": "MAGNESIUM",
    "POT": "POTASSIUM", "BICARB": "BICARBONATE", "CHLOR": "CHLORIDE",
    "PHOS": "PHOSPHATE", "HYDROCHLOR": "HYDROCHLORIDE", "CARB": "CARBONATE",
    "GLUC": "GLUCONATE", "TMP": "TRIMETHOPRIM", "SMX": "SULFAMETHOXAZOLE",
}

#: Elements a strength may be stated in terms of: `100 MG IRON/5 ML`, `320 MG
#: IODINE/ML`. The word qualifies which part of the molecule the number counts, and it
#: sits between the unit and the slash where a parser would otherwise stop.
STRENGTH_ELEMENTS: tuple[str, ...] = (
    "IODINE", "IRON", "CALCIUM", "PHOSPHORUS", "MAGNESIUM", "POTASSIUM",
    "SODIUM", "ZINC", "ELEMENTAL IRON", "ELEMENTAL CALCIUM", "BASE",
)

#: Release-rate abbreviations written next to the ingredient rather than the form.
RELEASE_ABBREVIATIONS: dict[str, str] = {
    "ER": "EXTENDED RELEASE", "XR": "EXTENDED RELEASE", "XL": "EXTENDED RELEASE",
    "SR": "EXTENDED RELEASE", "CR": "EXTENDED RELEASE", "DR": "DELAYED RELEASE",
    "EC": "DELAYED RELEASE",
}
