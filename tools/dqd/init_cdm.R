# Create an empty OMOP CDM 5.4 schema in PostgreSQL using the OHDSI DDL.
#
# The DDL comes from OHDSI/CommonDataModel rather than from this repository's own
# duckdb DDL, so that DQD's field-level datatype and requiredness checks are run
# against the column definitions upstream publishes, not against ours. If the two
# disagree, that disagreement should show up as a DQD failure rather than be defined
# away by handing DQD our own schema.
#
# Primary and foreign keys are deliberately NOT created. Half the faults under test
# are exactly the kind a constraint would reject at load time (a duplicated key, a
# clinical row pointing at a person who was withheld), and a load that refuses the
# corrupted data measures PostgreSQL, not DQD.

args <- commandArgs(trailingOnly = TRUE)
schema <- if (length(args) >= 1) args[[1]] else "cdm"

connectionDetails <- DatabaseConnector::createConnectionDetails(
  dbms = "postgresql",
  server = paste0(Sys.getenv("PGHOST"), "/", Sys.getenv("PGDATABASE")),
  user = Sys.getenv("PGUSER"),
  password = Sys.getenv("PGPASSWORD"),
  port = Sys.getenv("PGPORT")
)

CommonDataModel::executeDdl(
  connectionDetails = connectionDetails,
  cdmVersion = "5.4",
  cdmDatabaseSchema = schema,
  executeDdl = TRUE,
  executePrimaryKey = FALSE,
  executeForeignKey = FALSE
)

cat("cdm 5.4 ddl applied to schema ", schema, "\n", sep = "")
