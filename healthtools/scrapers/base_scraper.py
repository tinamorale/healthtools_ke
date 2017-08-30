import argparse
import boto3
import getpass
import hashlib
import json
import logging
import os
import progressbar
import re
import requests
import sys
import time

from bs4 import BeautifulSoup
from elasticsearch import Elasticsearch, RequestsHttpConnection

from cStringIO import StringIO
from datetime import datetime
from requests_aws4auth import AWS4Auth
from termcolor import colored

from logging.config import fileConfig

from healthtools.config import AWS, ES, SLACK, DATA_DIR, SMALL_BATCH, NHIF_SERVICES
from healthtools.lib.json_serializer import JSONSerializerPython2

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

ini_file = BASE_DIR + "/logging.ini"
fileConfig(ini_file)
logger = logging.getLogger(__name__)


class Scraper(object):
    '''
    Base Scraper:
    -------------
    This is the default scraper inherited by the rest.
    '''

    def __init__(self):

        parser = argparse.ArgumentParser()
        parser.add_argument('-sb', '--small_batch', action="store_true",
                            help="Specify option to scrape limited pages from site in development mode")

        args = parser.parse_args()

        if args.small_batch:
            logger.info('Using small batch')
            self.small_batch = True
        else:
            self.small_batch = False

        self.site_url = None
        self.site_pages_no = None
        self.fields = None

        self.doc_id = 1  # Id for each entry, to be incremented
        self.es_index = ES["index"]  # Elasticsearch index
        self.es_doc = None  # Elasticsearch doc_type

        self.s3 = boto3.client("s3", **{
            "aws_access_key_id": AWS["aws_access_key_id"],
            "aws_secret_access_key": AWS["aws_secret_access_key"],
            "region_name": AWS["region_name"]
        })

        self.data_key = DATA_DIR + "data.json"  # Storage key for latest data
        # Storage key for data to archive
        self.data_archive_key = DATA_DIR + "archive/data-{}.json"

        try:
            # client host for aws elastic search service
            if "aws" in ES["host"]:
                # set up authentication credentials
                awsauth = AWS4Auth(
                    AWS["aws_access_key_id"], AWS["aws_secret_access_key"], AWS["region_name"], "es")
                self.es_client = Elasticsearch(
                    hosts=[{"host": ES["host"], "port": int(ES["port"])}],
                    http_auth=awsauth,
                    use_ssl=True,
                    verify_certs=True,
                    connection_class=RequestsHttpConnection,
                    serializer=JSONSerializerPython2()
                )

            else:
                self.es_client = Elasticsearch(
                    "{}:{}".format(ES["host"], ES["port"]))
        except Exception as err:
            self.print_error(
                "- ERROR: ES Client Set Up \n- SOURCE: Invalid parameters for ES Client \n- MESSAGE: {}".
                format(str(err)))

        self.results = []
        self.results_es = []

    def run_scraper(self):
        '''
        This function works to display some output and run scrape_site()
        '''
        scraper_name = re.sub(r"(\w)([A-Z])", r"\1 \2", type(self).__name__)
        logger.info("%s started" % scraper_name)

        self.scrape_site()

        logger.info("{} completed. {} documents retrieved.".format(
            scraper_name, len(self.results)))

        return self.results

    def scrape_site(self):
        '''
        This functions scrapes the entire website by calling each page.
        '''
        self.set_site_pages_no()
        if not self.site_pages_no:
            self.print_error(
                "- ERROR: scrape_site() \n- SOURCE: {} \n- MESSAGE: {}"
                .format(self.site_url, "No pages found.")
            )
            return

        scraper_name = re.sub(r"(\w)([A-Z])", r"\1 \2", type(self).__name__)
        widgets = [' [', scraper_name, ': ',
                   progressbar.Timer(), '] ',
                   progressbar.Bar(marker='#', left='[', right=']'),
                   ' (', progressbar.ETA(), ' ', progressbar.FileTransferSpeed(), ') '
                   ]
        pbar = progressbar.ProgressBar(widgets=widgets).start()

        for page_num in pbar(range(1, self.site_pages_no + 1)):
            # Check if is NHIF and if so just use page_num else format site_url
            nhif = set(re.sub(r"(\w)([A-Z])", r"\1 \2", type(self).__name__).lower().split()) &\
                set(NHIF_SERVICES)

            url = page_num if nhif else self.site_url.format(page_num)

            results, results_es = self.scrape_page(url, 5)

            if type(results) != list:
                self.print_error("- ERROR: scrape_site() \n- SOURCE: {} \n-MESSAGE: page: {} \ndata: {}".
                                 format(url, page_num, results))
                return

            self.results.extend(results)
            self.results_es.extend(results_es)

        if self.results:
            self.archive_data(json.dumps(self.results))
            self.elasticsearch_delete_docs()
            self.elasticsearch_index(self.results_es)

        return self.results

    def scrape_page(self, page_url, page_retries):
        '''
        Scrape the page for the data.
        '''
        try:
            soup = self.make_soup(page_url)
            table = soup.find("table", {"class": "zebra"}).find("tbody")
            rows = table.find_all("tr")

            results = []
            results_es = []
            for row in rows:
                # only the columns we want
                # -1 because fields/columns has extra index; id
                columns = row.find_all("td")[:len(self.fields) - 1]
                columns = [text.text.strip() for text in columns]
                columns.append(self.doc_id)

                entry = dict(zip(self.fields, columns))
                meta, entry = self.elasticsearch_format(entry)
                results_es.append(meta)
                results_es.append(entry)
                results.append(entry)

                self.doc_id += 1

            return results, results_es

        except Exception as err:
            if page_retries >= 5:
                self.print_error(
                    "- ERROR: scrape_page() \n- SOURCE: {} \n- MESSAGE: {}".format(page_url, str(err)))
                return
            else:
                page_retries += 1
                self.print_error(
                    "- ERROR: Try {}/5 has failed... \n- SOURCE: {} \n- MESSAGE {} \nGoing to sleep for {} seconds.".
                    format(page_retries, page_url, err, page_retries * 5))
                time.sleep(page_retries * 5)
                self.scrape_page(page_url, page_retries)

    def set_site_pages_no(self):
        '''
        Set the total number of pages to be scraped
        '''
        try:
            soup = self.make_soup(self.site_url.format(1))
            text = soup.find("div", {"id": "tnt_pagination"}).getText()
            # What number of pages looks like
            pattern = re.compile("(\d+) pages?")
            self.site_pages_no = int(pattern.search(text).group(1))
        except Exception as err:
            self.print_error("- ERROR: get_total_page_numbers() \n- SOURCE: {} \n- MESSAGE: {}".
                             format(self.site_url, str(err)))

        # If small batch is set, that would be the number of pages.
        if self.small_batch and self.site_pages_no and self.site_pages_no > SMALL_BATCH:
            self.site_pages_no = SMALL_BATCH

        # TODO: Print how many pages we found

    def make_soup(self, url):
        '''
        Get page, make and return a BeautifulSoup object
        '''
        response = requests.get(url)
        soup = BeautifulSoup(response.content, "html.parser")
        return soup

    def elasticsearch_format(self, entry):
        """
        Format entry into elasticsearch ready document
        :param entry: the data to be formatted
        :return: dictionaries of the entry's metadata and the formatted entry
        """
        # all bulk data need meta data describing the data
        meta_dict = {
            "index": {
                "_index": self.es_index,
                "_type": self.es_doc,
                "_id": entry["id"]
            }
        }
        return meta_dict, entry

    def elasticsearch_index(self, results):
        '''
        Upload data to Elastic Search
        '''
        try:
            # sanity check
            if not self.es_client.indices.exists(index=self.es_index):
                self.es_client.indices.create(index=self.es_index)
                logger.info("Elasticsearch: Index successfully created")

            # bulk index the data and use refresh to ensure that our data will
            # be immediately available
            response = self.es_client.bulk(
                index=self.es_index, body=results, refresh=True)
            logger.info("Elasticsearch: Index successfully created")
            return response
        except Exception as err:
            self.print_error("- ERROR: elasticsearch_index() \n- SOURCE: {} \n- MESSAGE: {}".
                             format(type(self).__name__, str(err)))

    def elasticsearch_delete_docs(self):
        '''
        Delete documents that were uploaded to elasticsearch in the last scrape
        '''
        try:
            delete_query = {"query": {"match_all": {}}}
            try:
                response = self.es_client.delete_by_query(
                    index=self.es_index, doc_type=self.es_doc, body=delete_query, _source=True)
                return response
            except Exception as err:
                self.print_error("- ERROR: elasticsearch_delete_docs() \n- SOURCE: {} \n- MESSAGE: {}".
                                 format(type(self).__name__, str(err)))

        except Exception as err:
            self.print_error("- ERROR: elasticsearch_delete_docs() \n- SOURCE: {} \n- MESSAGE: {}".
                             format(type(self).__name__, str(err)))

    def archive_data(self, payload):
        '''
        Upload scraped data to AWS S3
        '''
        try:
            date = datetime.today().strftime("%Y%m%d")
            self.data_key = DATA_DIR + self.data_key
            self.data_archive_key = DATA_DIR + self.data_archive_key
            if AWS["s3_bucket"]:
                old_etag = self.s3.get_object(
                    Bucket=AWS["s3_bucket"], Key=self.data_key)["ETag"]
                new_etag = hashlib.md5(payload.encode("utf-8")).hexdigest()
                if eval(old_etag) != new_etag:
                    file_obj = StringIO(payload.encode("utf-8"))
                    self.s3.upload_fileobj(file_obj,
                                           AWS["s3_bucket"], self.data_key)

                    # archive historical data
                    self.s3.copy_object(Bucket=AWS["s3_bucket"],
                                        CopySource="{}/".format(
                                            AWS["s3_bucket"]) + self.data_key,
                                        Key=self.data_archive_key.format(date))
                    logger.info("Archive: Data has been updated")
                    return
                else:
                    logger.info(
                        "Archive: Data scraped does not differ from archived data")
            else:
                # archive to local dir
                with open(self.data_key, "w") as data:
                    json.dump(payload, data)
                # archive historical data to local dir
                with open(self.data_archive_key.format(date), "w") as history:
                    json.dump(payload, history)
                logger.info("Archived: Data has been updated")

        except Exception as err:
            self.print_error(
                "- ERROR: archive_data() \n- SOURCE: {} \n- MESSAGE: {}".format(self.data_key, str(err)))

    def print_error(self, message):
        '''
        Print error messages in the terminal.
        If slack webhook is set up, post the errors to Slack.
        '''
        print colored("[{0}]\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")) + message, "red")
        response = None
        if SLACK["url"]:
            try:
                err = message.split("-", 3)
                severity = err[3].split(":")[1]
                errors = {
                    "author": err[1].replace("ERROR:", "").strip(),
                    "pretext": err[2].replace("SOURCE:", "").strip(),
                    "message": err[3].replace("MESSAGE:", "").strip(),
                    "severity": severity
                }
            except:
                errors = {
                    "pretext": "",
                    "author": message,
                    "message": message,
                    "severity": message
                }
            response = requests.post(
                SLACK["url"],
                data=json.dumps({
                    "attachments": [
                        {
                            "author_name": "{}".format(errors["author"]),
                            "color": "danger",
                            "pretext": "[SCRAPER] New Alert for {} : {}".format(errors["author"], errors["pretext"]),
                            "fields": [
                                {
                                    "title": "Message",
                                    "value": "{}".format(errors["message"]),
                                    "short": False
                                },
                                {
                                    "title": "Machine Location",
                                    "value": "{}".format(getpass.getuser()),
                                    "short": True
                                },
                                {
                                    "title": "Time",
                                    "value": "{}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                                    "short": True},
                                {
                                    "title": "Severity",
                                    "value": "{}".format(errors["severity"]),
                                    "short": True
                                }
                            ]
                        }
                    ]
                }),
                headers={"Content-Type": "application/json"}
            )
        return response
