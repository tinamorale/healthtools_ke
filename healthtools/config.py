import os

# sites to be scraped
SITES = {
    "DOCTORS": "http://medicalboard.co.ke/online-services/retention/?currpage={}",
    "FOREIGN_DOCTORS": "http://medicalboard.co.ke/online-services/foreign-doctors-license-register/?currpage={}",
    "CLINICAL_OFFICERS": "http://clinicalofficerscouncil.org/online-services/retention/?currpage={}",
    "TOKEN_URL": "http://api.kmhfl.health.go.ke/o/token/"
    }

AWS = {
    "aws_access_key_id": os.getenv("MORPH_AWS_ACCESS_KEY_ID"),
    "aws_secret_access_key": os.getenv("MORPH_AWS_SECRET_KEY"),
    "region_name": os.getenv("MORPH_AWS_REGION")
    }
ES = {
    "host": os.getenv("ES_HOST"),
    "port": os.getenv("ES_PORT"),
    "index": "healthtools"
    }

TEST_DIR = os.getcwd() + "/healthtools/tests"

SLACK = {
    "url": os.getenv("WEBHOOK_URL")
    }
