# OMOP CDM 5.4 — field reference

Derived from the pinned DDL in `sql/omop_5.4/` (see `SOURCE.md` for the commit).
Regenerate with `python3 tools/make_standards_reference.py`. Never hand-edited: if this
disagrees with the DDL, the DDL is right.

`NOT NULL` matters more than it looks. Half the decisions in `src/ehr2cdm/omop.py` are
about what to do when the source cannot supply a required field, and the answer is never
to invent one.

## person

| column | type | null |
|---|---|---|
| `person_id` | integer | **no** |
| `gender_concept_id` | integer | **no** |
| `year_of_birth` | integer | **no** |
| `month_of_birth` | integer | yes |
| `day_of_birth` | integer | yes |
| `birth_datetime` | TIMESTAMP | yes |
| `race_concept_id` | integer | **no** |
| `ethnicity_concept_id` | integer | **no** |
| `location_id` | integer | yes |
| `provider_id` | integer | yes |
| `care_site_id` | integer | yes |
| `person_source_value` | varchar(50) | yes |
| `gender_source_value` | varchar(50) | yes |
| `gender_source_concept_id` | integer | yes |
| `race_source_value` | varchar(50) | yes |
| `race_source_concept_id` | integer | yes |
| `ethnicity_source_value` | varchar(50) | yes |
| `ethnicity_source_concept_id` | integer | yes |

## observation_period

| column | type | null |
|---|---|---|
| `observation_period_id` | integer | **no** |
| `person_id` | integer | **no** |
| `observation_period_start_date` | date | **no** |
| `observation_period_end_date` | date | **no** |
| `period_type_concept_id` | integer | **no** |

## visit_occurrence

| column | type | null |
|---|---|---|
| `visit_occurrence_id` | integer | **no** |
| `person_id` | integer | **no** |
| `visit_concept_id` | integer | **no** |
| `visit_start_date` | date | **no** |
| `visit_start_datetime` | TIMESTAMP | yes |
| `visit_end_date` | date | **no** |
| `visit_end_datetime` | TIMESTAMP | yes |
| `visit_type_concept_id` | Integer | **no** |
| `provider_id` | integer | yes |
| `care_site_id` | integer | yes |
| `visit_source_value` | varchar(50) | yes |
| `visit_source_concept_id` | integer | yes |
| `admitted_from_concept_id` | integer | yes |
| `admitted_from_source_value` | varchar(50) | yes |
| `discharged_to_concept_id` | integer | yes |
| `discharged_to_source_value` | varchar(50) | yes |
| `preceding_visit_occurrence_id` | integer | yes |

## visit_detail

| column | type | null |
|---|---|---|
| `visit_detail_id` | integer | **no** |
| `person_id` | integer | **no** |
| `visit_detail_concept_id` | integer | **no** |
| `visit_detail_start_date` | date | **no** |
| `visit_detail_start_datetime` | TIMESTAMP | yes |
| `visit_detail_end_date` | date | **no** |
| `visit_detail_end_datetime` | TIMESTAMP | yes |
| `visit_detail_type_concept_id` | integer | **no** |
| `provider_id` | integer | yes |
| `care_site_id` | integer | yes |
| `visit_detail_source_value` | varchar(50) | yes |
| `visit_detail_source_concept_id` | integer | yes |
| `admitted_from_concept_id` | integer | yes |
| `admitted_from_source_value` | varchar(50) | yes |
| `discharged_to_source_value` | varchar(50) | yes |
| `discharged_to_concept_id` | integer | yes |
| `preceding_visit_detail_id` | integer | yes |
| `parent_visit_detail_id` | integer | yes |
| `visit_occurrence_id` | integer | **no** |

## condition_occurrence

| column | type | null |
|---|---|---|
| `condition_occurrence_id` | integer | **no** |
| `person_id` | integer | **no** |
| `condition_concept_id` | integer | **no** |
| `condition_start_date` | date | **no** |
| `condition_start_datetime` | TIMESTAMP | yes |
| `condition_end_date` | date | yes |
| `condition_end_datetime` | TIMESTAMP | yes |
| `condition_type_concept_id` | integer | **no** |
| `condition_status_concept_id` | integer | yes |
| `stop_reason` | varchar(20) | yes |
| `provider_id` | integer | yes |
| `visit_occurrence_id` | integer | yes |
| `visit_detail_id` | integer | yes |
| `condition_source_value` | varchar(50) | yes |
| `condition_source_concept_id` | integer | yes |
| `condition_status_source_value` | varchar(50) | yes |

## drug_exposure

| column | type | null |
|---|---|---|
| `drug_exposure_id` | integer | **no** |
| `person_id` | integer | **no** |
| `drug_concept_id` | integer | **no** |
| `drug_exposure_start_date` | date | **no** |
| `drug_exposure_start_datetime` | TIMESTAMP | yes |
| `drug_exposure_end_date` | date | **no** |
| `drug_exposure_end_datetime` | TIMESTAMP | yes |
| `verbatim_end_date` | date | yes |
| `drug_type_concept_id` | integer | **no** |
| `stop_reason` | varchar(20) | yes |
| `refills` | integer | yes |
| `quantity` | NUMERIC | yes |
| `days_supply` | integer | yes |
| `sig` | TEXT | yes |
| `route_concept_id` | integer | yes |
| `lot_number` | varchar(50) | yes |
| `provider_id` | integer | yes |
| `visit_occurrence_id` | integer | yes |
| `visit_detail_id` | integer | yes |
| `drug_source_value` | varchar(50) | yes |
| `drug_source_concept_id` | integer | yes |
| `route_source_value` | varchar(50) | yes |
| `dose_unit_source_value` | varchar(50) | yes |

## procedure_occurrence

| column | type | null |
|---|---|---|
| `procedure_occurrence_id` | integer | **no** |
| `person_id` | integer | **no** |
| `procedure_concept_id` | integer | **no** |
| `procedure_date` | date | **no** |
| `procedure_datetime` | TIMESTAMP | yes |
| `procedure_end_date` | date | yes |
| `procedure_end_datetime` | TIMESTAMP | yes |
| `procedure_type_concept_id` | integer | **no** |
| `modifier_concept_id` | integer | yes |
| `quantity` | integer | yes |
| `provider_id` | integer | yes |
| `visit_occurrence_id` | integer | yes |
| `visit_detail_id` | integer | yes |
| `procedure_source_value` | varchar(50) | yes |
| `procedure_source_concept_id` | integer | yes |
| `modifier_source_value` | varchar(50) | yes |

## device_exposure

| column | type | null |
|---|---|---|
| `device_exposure_id` | integer | **no** |
| `person_id` | integer | **no** |
| `device_concept_id` | integer | **no** |
| `device_exposure_start_date` | date | **no** |
| `device_exposure_start_datetime` | TIMESTAMP | yes |
| `device_exposure_end_date` | date | yes |
| `device_exposure_end_datetime` | TIMESTAMP | yes |
| `device_type_concept_id` | integer | **no** |
| `unique_device_id` | varchar(255) | yes |
| `production_id` | varchar(255) | yes |
| `quantity` | integer | yes |
| `provider_id` | integer | yes |
| `visit_occurrence_id` | integer | yes |
| `visit_detail_id` | integer | yes |
| `device_source_value` | varchar(50) | yes |
| `device_source_concept_id` | integer | yes |
| `unit_concept_id` | integer | yes |
| `unit_source_value` | varchar(50) | yes |
| `unit_source_concept_id` | integer | yes |

## measurement

| column | type | null |
|---|---|---|
| `measurement_id` | integer | **no** |
| `person_id` | integer | **no** |
| `measurement_concept_id` | integer | **no** |
| `measurement_date` | date | **no** |
| `measurement_datetime` | TIMESTAMP | yes |
| `measurement_time` | varchar(10) | yes |
| `measurement_type_concept_id` | integer | **no** |
| `operator_concept_id` | integer | yes |
| `value_as_number` | NUMERIC | yes |
| `value_as_concept_id` | integer | yes |
| `unit_concept_id` | integer | yes |
| `range_low` | NUMERIC | yes |
| `range_high` | NUMERIC | yes |
| `provider_id` | integer | yes |
| `visit_occurrence_id` | integer | yes |
| `visit_detail_id` | integer | yes |
| `measurement_source_value` | varchar(50) | yes |
| `measurement_source_concept_id` | integer | yes |
| `unit_source_value` | varchar(50) | yes |
| `unit_source_concept_id` | integer | yes |
| `value_source_value` | varchar(50) | yes |
| `measurement_event_id` | integer | yes |
| `meas_event_field_concept_id` | integer | yes |

## observation

| column | type | null |
|---|---|---|
| `observation_id` | integer | **no** |
| `person_id` | integer | **no** |
| `observation_concept_id` | integer | **no** |
| `observation_date` | date | **no** |
| `observation_datetime` | TIMESTAMP | yes |
| `observation_type_concept_id` | integer | **no** |
| `value_as_number` | NUMERIC | yes |
| `value_as_string` | varchar(60) | yes |
| `value_as_concept_id` | integer | yes |
| `qualifier_concept_id` | integer | yes |
| `unit_concept_id` | integer | yes |
| `provider_id` | integer | yes |
| `visit_occurrence_id` | integer | yes |
| `visit_detail_id` | integer | yes |
| `observation_source_value` | varchar(50) | yes |
| `observation_source_concept_id` | integer | yes |
| `unit_source_value` | varchar(50) | yes |
| `qualifier_source_value` | varchar(50) | yes |
| `value_source_value` | varchar(50) | yes |
| `observation_event_id` | integer | yes |
| `obs_event_field_concept_id` | integer | yes |

## death

| column | type | null |
|---|---|---|
| `person_id` | integer | **no** |
| `death_date` | date | **no** |
| `death_datetime` | TIMESTAMP | yes |
| `death_type_concept_id` | integer | yes |
| `cause_concept_id` | integer | yes |
| `cause_source_value` | varchar(50) | yes |
| `cause_source_concept_id` | integer | yes |

## note

| column | type | null |
|---|---|---|
| `note_id` | integer | **no** |
| `person_id` | integer | **no** |
| `note_date` | date | **no** |
| `note_datetime` | TIMESTAMP | yes |
| `note_type_concept_id` | integer | **no** |
| `note_class_concept_id` | integer | **no** |
| `note_title` | varchar(250) | yes |
| `note_text` | TEXT | **no** |
| `encoding_concept_id` | integer | **no** |
| `language_concept_id` | integer | **no** |
| `provider_id` | integer | yes |
| `visit_occurrence_id` | integer | yes |
| `visit_detail_id` | integer | yes |
| `note_source_value` | varchar(50) | yes |
| `note_event_id` | integer | yes |
| `note_event_field_concept_id` | integer | yes |

## note_nlp

| column | type | null |
|---|---|---|
| `note_nlp_id` | integer | **no** |
| `note_id` | integer | **no** |
| `section_concept_id` | integer | yes |
| `snippet` | varchar(250) | yes |
| `"offset"` | varchar(50) | yes |
| `lexical_variant` | varchar(250) | **no** |
| `note_nlp_concept_id` | integer | yes |
| `note_nlp_source_concept_id` | integer | yes |
| `nlp_system` | varchar(250) | yes |
| `nlp_date` | date | **no** |
| `nlp_datetime` | TIMESTAMP | yes |
| `term_exists` | varchar(1) | yes |
| `term_temporal` | varchar(50) | yes |
| `term_modifiers` | varchar(2000) | yes |

## specimen

| column | type | null |
|---|---|---|
| `specimen_id` | integer | **no** |
| `person_id` | integer | **no** |
| `specimen_concept_id` | integer | **no** |
| `specimen_type_concept_id` | integer | **no** |
| `specimen_date` | date | **no** |
| `specimen_datetime` | TIMESTAMP | yes |
| `quantity` | NUMERIC | yes |
| `unit_concept_id` | integer | yes |
| `anatomic_site_concept_id` | integer | yes |
| `disease_status_concept_id` | integer | yes |
| `specimen_source_id` | varchar(50) | yes |
| `specimen_source_value` | varchar(50) | yes |
| `unit_source_value` | varchar(50) | yes |
| `anatomic_site_source_value` | varchar(50) | yes |
| `disease_status_source_value` | varchar(50) | yes |

## fact_relationship

| column | type | null |
|---|---|---|
| `domain_concept_id_1` | integer | **no** |
| `fact_id_1` | integer | **no** |
| `domain_concept_id_2` | integer | **no** |
| `fact_id_2` | integer | **no** |
| `relationship_concept_id` | integer | **no** |

## location

| column | type | null |
|---|---|---|
| `location_id` | integer | **no** |
| `address_1` | varchar(50) | yes |
| `address_2` | varchar(50) | yes |
| `city` | varchar(50) | yes |
| `state` | varchar(2) | yes |
| `zip` | varchar(9) | yes |
| `county` | varchar(20) | yes |
| `location_source_value` | varchar(50) | yes |
| `country_concept_id` | integer | yes |
| `country_source_value` | varchar(80) | yes |
| `latitude` | NUMERIC | yes |
| `longitude` | NUMERIC | yes |

## care_site

| column | type | null |
|---|---|---|
| `care_site_id` | integer | **no** |
| `care_site_name` | varchar(255) | yes |
| `place_of_service_concept_id` | integer | yes |
| `location_id` | integer | yes |
| `care_site_source_value` | varchar(50) | yes |
| `place_of_service_source_value` | varchar(50) | yes |

## provider

| column | type | null |
|---|---|---|
| `provider_id` | integer | **no** |
| `provider_name` | varchar(255) | yes |
| `npi` | varchar(20) | yes |
| `dea` | varchar(20) | yes |
| `specialty_concept_id` | integer | yes |
| `care_site_id` | integer | yes |
| `year_of_birth` | integer | yes |
| `gender_concept_id` | integer | yes |
| `provider_source_value` | varchar(50) | yes |
| `specialty_source_value` | varchar(50) | yes |
| `specialty_source_concept_id` | integer | yes |
| `gender_source_value` | varchar(50) | yes |
| `gender_source_concept_id` | integer | yes |

## payer_plan_period

| column | type | null |
|---|---|---|
| `payer_plan_period_id` | integer | **no** |
| `person_id` | integer | **no** |
| `payer_plan_period_start_date` | date | **no** |
| `payer_plan_period_end_date` | date | **no** |
| `payer_concept_id` | integer | yes |
| `payer_source_value` | varchar(50) | yes |
| `payer_source_concept_id` | integer | yes |
| `plan_concept_id` | integer | yes |
| `plan_source_value` | varchar(50) | yes |
| `plan_source_concept_id` | integer | yes |
| `sponsor_concept_id` | integer | yes |
| `sponsor_source_value` | varchar(50) | yes |
| `sponsor_source_concept_id` | integer | yes |
| `family_source_value` | varchar(50) | yes |
| `stop_reason_concept_id` | integer | yes |
| `stop_reason_source_value` | varchar(50) | yes |
| `stop_reason_source_concept_id` | integer | yes |

## cost

| column | type | null |
|---|---|---|
| `cost_id` | integer | **no** |
| `cost_event_id` | integer | **no** |
| `cost_domain_id` | varchar(20) | **no** |
| `cost_type_concept_id` | integer | **no** |
| `currency_concept_id` | integer | yes |
| `total_charge` | NUMERIC | yes |
| `total_cost` | NUMERIC | yes |
| `total_paid` | NUMERIC | yes |
| `paid_by_payer` | NUMERIC | yes |
| `paid_by_patient` | NUMERIC | yes |
| `paid_patient_copay` | NUMERIC | yes |
| `paid_patient_coinsurance` | NUMERIC | yes |
| `paid_patient_deductible` | NUMERIC | yes |
| `paid_by_primary` | NUMERIC | yes |
| `paid_ingredient_cost` | NUMERIC | yes |
| `paid_dispensing_fee` | NUMERIC | yes |
| `payer_plan_period_id` | integer | yes |
| `amount_allowed` | NUMERIC | yes |
| `revenue_code_concept_id` | integer | yes |
| `revenue_code_source_value` | varchar(50) | yes |
| `drg_concept_id` | integer | yes |
| `drg_source_value` | varchar(3) | yes |

## drug_era

| column | type | null |
|---|---|---|
| `drug_era_id` | integer | **no** |
| `person_id` | integer | **no** |
| `drug_concept_id` | integer | **no** |
| `drug_era_start_date` | date | **no** |
| `drug_era_end_date` | date | **no** |
| `drug_exposure_count` | integer | yes |
| `gap_days` | integer | yes |

## dose_era

| column | type | null |
|---|---|---|
| `dose_era_id` | integer | **no** |
| `person_id` | integer | **no** |
| `drug_concept_id` | integer | **no** |
| `unit_concept_id` | integer | **no** |
| `dose_value` | NUMERIC | **no** |
| `dose_era_start_date` | date | **no** |
| `dose_era_end_date` | date | **no** |

## condition_era

| column | type | null |
|---|---|---|
| `condition_era_id` | integer | **no** |
| `person_id` | integer | **no** |
| `condition_concept_id` | integer | **no** |
| `condition_era_start_date` | date | **no** |
| `condition_era_end_date` | date | **no** |
| `condition_occurrence_count` | integer | yes |

## episode

| column | type | null |
|---|---|---|
| `episode_id` | integer | **no** |
| `person_id` | integer | **no** |
| `episode_concept_id` | integer | **no** |
| `episode_start_date` | date | **no** |
| `episode_start_datetime` | TIMESTAMP | yes |
| `episode_end_date` | date | yes |
| `episode_end_datetime` | TIMESTAMP | yes |
| `episode_parent_id` | integer | yes |
| `episode_number` | integer | yes |
| `episode_object_concept_id` | integer | **no** |
| `episode_type_concept_id` | integer | **no** |
| `episode_source_value` | varchar(50) | yes |
| `episode_source_concept_id` | integer | yes |

## episode_event

| column | type | null |
|---|---|---|
| `episode_id` | integer | **no** |
| `event_id` | integer | **no** |
| `episode_event_field_concept_id` | integer | **no** |

## metadata

| column | type | null |
|---|---|---|
| `metadata_id` | integer | **no** |
| `metadata_concept_id` | integer | **no** |
| `metadata_type_concept_id` | integer | **no** |
| `name` | varchar(250) | **no** |
| `value_as_string` | varchar(250) | yes |
| `value_as_concept_id` | integer | yes |
| `value_as_number` | NUMERIC | yes |
| `metadata_date` | date | yes |
| `metadata_datetime` | TIMESTAMP | yes |

## cdm_source

| column | type | null |
|---|---|---|
| `cdm_source_name` | varchar(255) | **no** |
| `cdm_source_abbreviation` | varchar(25) | **no** |
| `cdm_holder` | varchar(255) | **no** |
| `source_description` | TEXT | yes |
| `source_documentation_reference` | varchar(255) | yes |
| `cdm_etl_reference` | varchar(255) | yes |
| `source_release_date` | date | **no** |
| `cdm_release_date` | date | **no** |
| `cdm_version` | varchar(10) | yes |
| `cdm_version_concept_id` | integer | **no** |
| `vocabulary_version` | varchar(20) | **no** |

## concept

| column | type | null |
|---|---|---|
| `concept_id` | integer | **no** |
| `concept_name` | varchar(255) | **no** |
| `domain_id` | varchar(20) | **no** |
| `vocabulary_id` | varchar(20) | **no** |
| `concept_class_id` | varchar(20) | **no** |
| `standard_concept` | varchar(1) | yes |
| `concept_code` | varchar(50) | **no** |
| `valid_start_date` | date | **no** |
| `valid_end_date` | date | **no** |
| `invalid_reason` | varchar(1) | yes |

## vocabulary

| column | type | null |
|---|---|---|
| `vocabulary_id` | varchar(20) | **no** |
| `vocabulary_name` | varchar(255) | **no** |
| `vocabulary_reference` | varchar(255) | yes |
| `vocabulary_version` | varchar(255) | yes |
| `vocabulary_concept_id` | integer | **no** |

## domain

| column | type | null |
|---|---|---|
| `domain_id` | varchar(20) | **no** |
| `domain_name` | varchar(255) | **no** |
| `domain_concept_id` | integer | **no** |

## concept_class

| column | type | null |
|---|---|---|
| `concept_class_id` | varchar(20) | **no** |
| `concept_class_name` | varchar(255) | **no** |
| `concept_class_concept_id` | integer | **no** |

## concept_relationship

| column | type | null |
|---|---|---|
| `concept_id_1` | integer | **no** |
| `concept_id_2` | integer | **no** |
| `relationship_id` | varchar(20) | **no** |
| `valid_start_date` | date | **no** |
| `valid_end_date` | date | **no** |
| `invalid_reason` | varchar(1) | yes |

## relationship

| column | type | null |
|---|---|---|
| `relationship_id` | varchar(20) | **no** |
| `relationship_name` | varchar(255) | **no** |
| `is_hierarchical` | varchar(1) | **no** |
| `defines_ancestry` | varchar(1) | **no** |
| `reverse_relationship_id` | varchar(20) | **no** |
| `relationship_concept_id` | integer | **no** |

## concept_synonym

| column | type | null |
|---|---|---|
| `concept_id` | integer | **no** |
| `concept_synonym_name` | varchar(1000) | **no** |
| `language_concept_id` | integer | **no** |

## concept_ancestor

| column | type | null |
|---|---|---|
| `ancestor_concept_id` | integer | **no** |
| `descendant_concept_id` | integer | **no** |
| `min_levels_of_separation` | integer | **no** |
| `max_levels_of_separation` | integer | **no** |

## source_to_concept_map

| column | type | null |
|---|---|---|
| `source_code` | varchar(50) | **no** |
| `source_concept_id` | integer | **no** |
| `source_vocabulary_id` | varchar(20) | **no** |
| `source_code_description` | varchar(255) | yes |
| `target_concept_id` | integer | **no** |
| `target_vocabulary_id` | varchar(20) | **no** |
| `valid_start_date` | date | **no** |
| `valid_end_date` | date | **no** |
| `invalid_reason` | varchar(1) | yes |

## drug_strength

| column | type | null |
|---|---|---|
| `drug_concept_id` | integer | **no** |
| `ingredient_concept_id` | integer | **no** |
| `amount_value` | NUMERIC | yes |
| `amount_unit_concept_id` | integer | yes |
| `numerator_value` | NUMERIC | yes |
| `numerator_unit_concept_id` | integer | yes |
| `denominator_value` | NUMERIC | yes |
| `denominator_unit_concept_id` | integer | yes |
| `box_size` | integer | yes |
| `valid_start_date` | date | **no** |
| `valid_end_date` | date | **no** |
| `invalid_reason` | varchar(1) | yes |

## cohort

| column | type | null |
|---|---|---|
| `cohort_definition_id` | integer | **no** |
| `subject_id` | integer | **no** |
| `cohort_start_date` | date | **no** |
| `cohort_end_date` | date | **no** |

## cohort_definition

| column | type | null |
|---|---|---|
| `cohort_definition_id` | integer | **no** |
| `cohort_definition_name` | varchar(255) | **no** |
| `cohort_definition_description` | TEXT | yes |
| `definition_type_concept_id` | integer | **no** |
| `cohort_definition_syntax` | TEXT | yes |
| `subject_concept_id` | integer | **no** |
| `cohort_initiation_date` | date | yes |
