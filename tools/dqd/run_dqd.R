# Run one full DQD pass over the CDM schema and drop the raw result JSON in /out.
#
# Invoked once per build under test (the clean baseline, then each injected fault).
# Nothing is filtered or thresholded here: whatever DQD says is what gets written, and
# the Python harness does the comparison. A run that quietly dropped an inconvenient
# check would make the whole measurement worthless.

args <- commandArgs(trailingOnly = TRUE)
runLabel <- if (length(args) >= 1) args[[1]] else "run"
schema <- if (length(args) >= 2) args[[2]] else "cdm"
outputFolder <- if (length(args) >= 3) args[[3]] else "/out"

connectionDetails <- DatabaseConnector::createConnectionDetails(
  dbms = "postgresql",
  server = paste0(Sys.getenv("PGHOST"), "/", Sys.getenv("PGDATABASE")),
  user = Sys.getenv("PGUSER"),
  password = Sys.getenv("PGPASSWORD"),
  port = Sys.getenv("PGPORT")
)

dir.create(outputFolder, showWarnings = FALSE, recursive = TRUE)

# writeToTable = FALSE: the results schema would persist between runs and the next
# run's comparison would be reading the previous run's rows.
results <- DataQualityDashboard::executeDqChecks(
  connectionDetails = connectionDetails,
  cdmDatabaseSchema = schema,
  vocabDatabaseSchema = schema,
  resultsDatabaseSchema = schema,
  cdmSourceName = runLabel,
  cdmVersion = "5.4",
  numThreads = 1,
  sqlOnly = FALSE,
  outputFolder = outputFolder,
  outputFile = paste0(runLabel, ".json"),
  writeToTable = FALSE,
  writeToCsv = FALSE,
  verboseMode = FALSE,
  checkLevels = c("TABLE", "FIELD", "CONCEPT")
)

# Recorded alongside the results because "DQD found nothing" is only a claim about
# DQD if the version that found nothing is on the record.
versions <- list(
  DataQualityDashboard = as.character(utils::packageVersion("DataQualityDashboard")),
  DatabaseConnector = as.character(utils::packageVersion("DatabaseConnector")),
  SqlRender = as.character(utils::packageVersion("SqlRender")),
  CommonDataModel = as.character(utils::packageVersion("CommonDataModel")),
  R = paste0(R.version$major, ".", R.version$minor)
)
writeLines(
  jsonlite::toJSON(versions, auto_unbox = TRUE, pretty = TRUE),
  file.path(outputFolder, "versions.json")
)

cat("dqd finished: ", nrow(results$CheckResults), " checks\n", sep = "")
