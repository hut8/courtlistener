# coding=utf-8
import json
import os
import pickle

from django.apps import apps
from django.conf import settings
from django.core.files.base import ContentFile
from juriscraper.lasc.fetch import LASCSearch
from juriscraper.lasc.http import LASCSession

from cl.celery import app
from cl.lasc.models import Docket, DocumentImage, QueuedCase, QueuedPDF, \
    UPLOAD_TYPE
from cl.lasc.models import LASCJSON, LASCPDF
from cl.lib.command_utils import logger
from cl.lib.crypto import sha1_of_json_data
from cl.lib.redis_utils import make_redis_interface

LASC_USERNAME = os.environ.get('LASC_USERNAME', settings.LASC_USERNAME)
LASC_PASSWORD = os.environ.get('LASC_PASSWORD', settings.LASC_PASSWORD)


LASC_SESSION_STATUS_KEY = "session:lasc:status"
LASC_SESSION_COOKIE_KEY = "session:lasc:cookies"


def login_to_court():
    """
    Update session:lasc:status & session:lasc:cookies
    Log into MAP and reset status and cookies

    :return:
    """
    # TODO Explain why timeouts are the length they are
    r = make_redis_interface('CACHE')
    r.set(LASC_SESSION_STATUS_KEY, "False", ex=60 * 5)
    lasc_session = LASCSession(username=LASC_USERNAME,
                               password=LASC_PASSWORD)
    lasc_session.login()
    cookie_str = str(pickle.dumps(lasc_session.cookies))
    r.set(LASC_SESSION_COOKIE_KEY, cookie_str, ex=60 * 30)
    r.set(LASC_SESSION_STATUS_KEY, "True", ex=60 * 30)


def get_lasc_session():
    """
    Returns a LASC Session with stored cookies

    :return:
    """
    session = LASCSession(username=LASC_USERNAME,
                          password=LASC_PASSWORD)
    r = make_redis_interface('CACHE')
    pickled_cookies = r.get(LASC_SESSION_COOKIE_KEY)
    session.cookies = pickle.loads(pickled_cookies)
    return session


@app.task(bind=True, ignore_result=True, max_retries=1)
def download_pdf(self, pdf):
    """
    Downloads the PDF associated with the PDF DB Object passed in.

    :param self:
    :param pdf:
    :return:
    """
    check_login_status(self)
    lasc_session = get_lasc_session()
    lasc = LASCSearch(lasc_session)

    doc = DocumentImage.objects.get(doc_id=pdf.document_id)
    if not doc.is_available:
        pdf_data = lasc.get_pdf_from_url(pdf.document_url)

        pdf_document = LASCPDF(
            content_object=pdf,
            docket_number=pdf.docket.case_id.split(";")[0],
            document_id=pdf.document_id
        )

        pdf_document.filepath.save(
            pdf.document_id,
            ContentFile(pdf_data),
        )

        doc = DocumentImage.objects.get(doc_id=pdf.document_id)
        doc.is_available = True
        doc.save()

        pdf.delete()
    else:
        logger.info("Already have LASC PDF from docket ID %s with doc ID "
                    "%s ", doc.docket_id, doc.doc_id)


def add_case(case_id, case_data, lasc):
    """
    Adds a new case to the cl.lasc database

    :param case_id:
    :param case_data:
    :param lasc:
    :return:
    """
    is_queued = QueuedCase.objects.filter(internal_case_id=case_id)

    if is_queued.count() == 1:
        case_data["Docket"]['judge_code'] = is_queued[0].judge_code
        case_data["Docket"]['case_type_code'] = is_queued[0].case_type_code

    docket = Docket.objects.create(**case_data["Docket"])

    models = [x for x in apps.get_app_config('lasc').get_models()
              if x.__name__ not in ["Docket"]]

    while models:
        mdl = models.pop()
        while case_data[mdl.__name__]:
            case_data_row = case_data[mdl.__name__].pop()
            case_data_row["docket"] = docket
            mdl.objects.create(**case_data_row).save()

    save_json(lasc.case_data, docket)

    if is_queued.count() == 1:
        is_queued[0].delete()


def check_login_status(self):
    """
    Checks the Login Status for LASC.  If no status is found runs log in
    function and restores cookies and status

    :rtype: object
    :param self:
    :return:
    """
    r = make_redis_interface('CACHE')
    good_login = bool(r.get(LASC_SESSION_STATUS_KEY))
    if good_login:
        # XXX note if/then logic flipped, good state goes first
        # XXX what does countdown do and why?
        self.retry(countdown=60)
    else:
        login_to_court()
        self.retry(countdown=60)
    elif fetch_redis('session:lasc:status') is "False":
        self.retry(countdown=60)


@app.task(bind=True, ignore_result=True, max_retries=1)
def add_or_update_case_db(self, case_id):
    """
    Add a case from the LASC MAP using an authenticated session object

    :param self
    :param case_id: The case ID to download, for example, '19STCV25157;SS;CV'
    :return: None
    """
    check_login_status(self)
    lasc_session = get_lasc_session()
    lasc = LASCSearch(lasc_session)

    clean_data = {}
    try:
        clean_data = lasc.get_json_from_internal_case_id(case_id)
        logger.info("Successful Query")
    except:
        # XXX this except has to spell out all exceptions unless we have a
        #  super good reason not to.
        # XXX maybe wrap all session cleanup code in little function?
        r = make_redis_interface('CACHE')
        r.delete(LASC_SESSION_COOKIE_KEY, LASC_SESSION_STATUS_KEY)
        self.retry(countdown=60)

    docket = Docket.objects.filter(case_id=case_id)
    count = docket.count()

    if count == 0:
        logger.info("Adding lasc case with ID: %s", case_id)
        add_case(case_id, clean_data, lasc)
    elif count == 1:
        if latest_sha(case_id=case_id) != sha1_of_json_data(lasc.case_data):
            logger.info("Updating lasc case with ID: %s", case_id)
            update_case(lasc, clean_data)
        else:
            logger.info("LASC case is already up to date: %s", case_id)
    else:
        logger.warn("Issue adding or updating lasc case with ID '%s' - Too "
                    "many cases in system with that ID", case_id)


def latest_sha(case_id):
    """
    Query latest sha1 for Case by case_id

    :param case_id:
    :return:
    """
    docket = Docket.objects.get(case_id=case_id)
    o_id = LASCJSON(content_object=docket).object_id
    return LASCJSON.objects.filter(object_id=o_id).order_by('-pk')[0].sha1


def update_case(lasc, clean_data):
    """
    This code should update cases that have detected changes
    Method currently deletes and replaces the data on the system except for
    lasc_docket and connections for older json and pdf files.

    :param lasc: A LASCSearch object
    :param clean_data: A normalized data dictionary
    :return: None
    """
    docket_number = clean_data['Docket']['docket_number']
    district = clean_data['Docket']['district']
    division_code = clean_data['Docket']['division_code']

    case_id = ";".join([docket_number, district, division_code])

    docket = Docket.objects.filter(case_id=case_id)[0]
    docket.__dict__.update(clean_data['Docket'])
    docket.save()

    docket = Docket.objects.filter(case_id=case_id)[0]

    models = [x for x in apps.get_app_config('lasc').get_models()
              if x.__name__ not in ["Docket", "QueuedPDF",
                                    "QueuedCase", "LASCPDF",
                                    "LASCJSON", "DocumentImage"]]

    while models:
        mdl = models.pop()
        print mdl.__name__

        while clean_data[mdl.__name__]:
            row = clean_data[mdl.__name__].pop()
            row['docket'] = docket
            mdl.objects.create(**row).save()

    documents = clean_data['DocumentImage']
    for row in documents:
        r = DocumentImage.objects.filter(doc_id=row['doc_id'])
        if r.count() == 1:
            row['is_available'] = r[0].is_available
            rr = r[0]
            rr.__dict__.update(**row)
            rr.save()
        else:
            row["docket"] = docket
            DocumentImage.objects.create(**row).save()

    logger.info("Finished updating lasc case '%s'", case_id)
    save_json(lasc.case_data, content_obj=docket)


def remove_case(case_id):
    dock_obj = Docket.objects.filter(case_id=case_id)
    dock_obj.delete()


@app.task(ignore_result=True, max_retries=1)
def add_case_from_filepath(fp):
    """
    Add case to database from filepath (fp)

    :param self:
    :param fp: Filepath string
    :return: None
    """

    fp = fp.replace('courtlistener', 'celery')

    query = LASCSearch(None)
    with open(fp, 'r') as f:
        case_data = f.read()

    clean_data = query._parse_case_data(json.loads(case_data))
    docket_number = clean_data['Docket']['docket_number']
    district = clean_data['Docket']['district']
    division_code = clean_data['Docket']['division_code']

    case_id = ";".join([docket_number, district, division_code])

    dock_obj = Docket.objects.filter(case_id=case_id)

    if dock_obj.count() == 0:
        is_queued = QueuedCase.objects.filter(internal_case_id=case_id)

        if is_queued.count() == 1:
            clean_data["Docket"]['judge_code'] = is_queued[0].judge_code
            clean_data["Docket"]['case_type_code'] = is_queued[
                0].case_type_code

        docket = Docket.objects.create(**clean_data["Docket"])
        docket.save()

        dock_obj = Docket.objects.filter(case_id=case_id)

        models = [x for x in apps.get_app_config('lasc').get_models()
                  if x.__name__ not in ["Docket"]]

        while models:
            mdl = models.pop()
            while clean_data[mdl.__name__]:
                row = clean_data[mdl.__name__].pop()
                row["docket"] = dock_obj[0]
                mdl.objects.create(**row).save()

        save_json(case_data, dock_obj[0])

        if is_queued.count() == 1:
            is_queued[0].delete()

    elif dock_obj.count() == 1:
        logger.warn("LASC case on file system at '%s' is already in "
                    "the database ", fp)


def fetch_case_list_by_date(start, end):
    """
    Search for cases by date and add them to the DB.

    :param start: The date you want to start searching for cases
    :type start: datetime.date
    :param end: The date you want to stop searching for cases
    :type end: datetime.date
    :return: None
    """
    from datetime import datetime, timedelta
    from dateutil.rrule import rrule, WEEKLY

    start = datetime(start.year, start.month, start.day)
    end = datetime(end.year, end.month, end.day)

    end = min(end, datetime.today())
    weekly_dates = rrule(freq=WEEKLY, dtstart=start, until=end)

    lastday = []
    for start in weekly_dates:
        seven_days_later = start + timedelta(days=7)
        end = min(seven_days_later, end)

        if end not in lastday:
            fetch_date_range.apply_async(
                kwargs={"start": start.strftime('%m-%d-%Y'),
                        "end": end.strftime('%m-%d-%Y')},
            )

        lastday.append(end)


@app.task(bind=True, ignore_result=True, max_retries=2)
def fetch_date_range(self, start, end):
    """
    Queries LASC for one week or less range and returns the cases filed.

    :param self:
    :param start: The date you want to start searching for cases
    :type start: string
    :param end: The date you want to stop searching for cases
    :type end: string
    :return:
    """
    check_login_status(self)
    lasc_session = get_lasc_session()
    lasc = LASCSearch(lasc_session)

    cases = lasc.query_cases_by_date(start, end)
    cases_added_cnt = 0
    for case in cases:
        internal_case_id = case['internal_case_id']
        case_object = QueuedCase.objects.filter(
            internal_case_id=internal_case_id)
        if not case_object.exists():
            QueuedCase.objects.create(**{
                'internal_case_id': internal_case_id,
                'judge_code': case['judge_code'],
                'case_type_code': case['case_type_code'],
            })
            cases_added_cnt += 1
            logger.info("Adding case '%s' to LASC database.",
                        case['internal_case_id'])

    logger.info("Added %s cases to the QueuedCase table.", cases_added_cnt)


def save_json(data, content_obj):
    """
    Save json string to file and generate SHA1.

    :param data: JSON response cleaned
    :param content_obj:
    :return:
    """
    json_file = LASCJSON(content_object=content_obj)
    json_file.sha1 = sha1_of_json_data(data)
    json_file.upload_type = UPLOAD_TYPE.DOCKET
    json_file.filepath.save(
        'lasc.json',
        ContentFile(data),
    )
