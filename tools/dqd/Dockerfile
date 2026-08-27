# Real OHDSI Data Quality Dashboard, pinned and built once.
#
# The comparison this image exists for is only worth anything if the DQD being run is
# the one an OHDSI site would run -- not a reimplementation of its checks in Python.
# So: upstream R package, upstream CDM DDL, upstream JDBC driver, talking to a real
# PostgreSQL over the same connection path a site uses.
#
# Built once and reused across all eighteen runs (clean baseline plus seventeen
# faults). Reinstalling the R stack per run would take longer than the experiment.
#
#   docker build -t ehr2cdm-dqd tools/dqd
#
# rocker/r-ver pins a CRAN snapshot, so the CRAN dependency set is reproducible; the
# four OHDSI packages are pinned to release tags below for the same reason. The
# resolved versions are recorded in the results JSON regardless, because a pin in a
# Dockerfile is a claim and the version the run actually used is the evidence.
FROM rocker/r-ver:4.4

ARG DQD_REF=v2.8.9
ARG CDM_REF=v5.4.3
# Not the newest tag: DatabaseConnector 7.x needs dbplyr >= 2.6.0, which the CRAN
# snapshot this base image pins does not carry. Left unpinned, the failure is silent --
# install_github warns, the layer succeeds, and DQD quietly picks up whatever CRAN
# version its own dependency resolution drags in. Pinned to the version that actually
# builds here, and asserted at the end of the build.
ARG DATABASECONNECTOR_REF=v6.4.0
ARG SQLRENDER_REF=v1.19.6

# DatabaseConnector reaches PostgreSQL over JDBC, so the image needs a JDK and a
# working rJava as well as R. The -dev packages are for the CRAN sources that are not
# available as binaries for this snapshot.
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jdk \
        libcurl4-openssl-dev \
        libssl-dev \
        libxml2-dev \
        libgit2-dev \
        libfontconfig1-dev \
        libharfbuzz-dev \
        libfribidi-dev \
        libfreetype6-dev \
        libpng-dev \
        libtiff5-dev \
        libjpeg-dev \
        git \
    && rm -rf /var/lib/apt/lists/* \
    && R CMD javareconf

RUN install2.r --error --ncpus -1 remotes rJava

RUN R -q -e "remotes::install_github('OHDSI/SqlRender', ref='${SQLRENDER_REF}', upgrade='never')" \
 && R -q -e "remotes::install_github('OHDSI/DatabaseConnector', ref='${DATABASECONNECTOR_REF}', upgrade='never')" \
 && R -q -e "remotes::install_github('OHDSI/CommonDataModel', ref='${CDM_REF}', upgrade='never')" \
 && R -q -e "remotes::install_github('OHDSI/DataQualityDashboard', ref='${DQD_REF}', upgrade='never')"

# Baked into the image rather than downloaded per run: the driver is a fixed artifact
# and pulling it eighteen times is eighteen chances for the network to decide the
# experiment's outcome.
ENV DATABASECONNECTOR_JAR_FOLDER=/opt/jdbc
RUN mkdir -p /opt/jdbc \
 && R -q -e "DatabaseConnector::downloadJdbcDrivers('postgresql', pathToDriver='/opt/jdbc')"

# install_github only warns when a package fails to build, so without this the image
# ships whatever survived and the experiment reports a version nobody chose.
RUN R -q -e "for (p in c('DataQualityDashboard','DatabaseConnector','SqlRender','CommonDataModel')) library(p, character.only = TRUE); \
  stopifnot(as.character(packageVersion('DataQualityDashboard')) == sub('^v', '', '${DQD_REF}'), \
            as.character(packageVersion('DatabaseConnector')) == sub('^v', '', '${DATABASECONNECTOR_REF}'), \
            as.character(packageVersion('SqlRender')) == sub('^v', '', '${SQLRENDER_REF}'))"

COPY init_cdm.R run_dqd.R /opt/dqd/
WORKDIR /opt/dqd
