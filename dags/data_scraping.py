from airflow import DAG
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, expect
from airflow.operators.python_operator import PythonOperator
from bs4 import BeautifulSoup
import json
import re
import logging
import yaml
from pymongo import MongoClient
from pymongo.server_api import ServerApi


def create_list_of_offert(ul_element):
    soup = BeautifulSoup(ul_element, 'html.parser')
    list_items = soup.find_all('li')
    list_elements = []
    for item in list_items:
        #Search article element
        article = item.find('article')
        if article is not None:
            #Initialize empty dictionary
            offert_info = {}
            #Get div element in article
            item_div_elements = article.find_all('div', recursive=False)
            location_information = article.find('p', recursive=False).text
            offert_title_div = item_div_elements[0]
            offert_title = offert_title_div.find('span').text
            offert_details = item_div_elements[1].find_all('span', recursive=False)
            price = offert_details[0].text
            surface = offert_details[3].text
            rooms = offert_details[2].text
            offert_info['offert_title'] = offert_title
            offert_info['location'] = location_information
            offert_info['price'] = price
            offert_info['surface'] = surface
            offert_info['rooms'] = rooms
            offert_info['ingested_at'] = str(datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
            list_elements.append(offert_info)
    return list_elements

def get_content(**kwargs):
    logging.info('Data scraping started!')
    with sync_playwright() as p:
        # Launch a browser
        browser = p.firefox.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto('https://www.otodom.pl')
            # Wait for cookies pop-up and then accept
            page.locator('#onetrust-accept-btn-handler').click()
            #Click location button
            page.locator('button#location').click()
            #Populate with data
            page.locator('#location-picker-input').fill('Kraków')
            #Select proper checkbox
            page.locator("li").filter(has_text="Kraków, małopolskiemiasto").get_by_test_id("checkbox").click()
            # search when all parameters will apply properly
            submit_button = page.locator('#search-form-submit')
            expect(submit_button).to_have_text(re.compile(r"\w+ [0-9]+"))
            #Click search button
            submit_button.click()
            #Initialize 1st page of pagination
            pagination_page = 1
            #Wait for offert selector
            offert_list = []
            while len(offert_list) <= 1000:
                if pagination_page == 1:
                    ul_element = page.locator("span:has-text('Wszystkie ogłoszenia') + ul").inner_html()
                    list_elements = create_list_of_offert(ul_element)
                    offert_list += list_elements
                    pagination_page += 1
                else:
                    #Click the next page
                    page.locator('[data-cy="pagination.next-page"]').click()
                    ul_element = page.locator("span:has-text('Wszystkie ogłoszenia') + ul").inner_html()
                    list_elements = create_list_of_offert(ul_element)
                    offert_list += list_elements
                    pagination_page += 1
        except Exception as err:
            browser.close()
            logging.error(f'Unknown error occur: {err}')
            raise
        browser.close()
    kwargs['ti'].xcom_push(key='shared_data', value=offert_list)
    return offert_list

def write_data_to_json_format(**kwargs):
    offert_list = kwargs['ti'].xcom_pull(task_ids='collect_data', key='shared_data')
    logging.info('Writing data to file')
    current_timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        filename = f'/opt/airflow/output_files/{current_timestamp}_output.json'
        # Push the filename to XCom
        kwargs['ti'].xcom_push(key='filename', value=filename)
        with open(filename, 'w') as file:
            file.write(json.dumps(offert_list, indent=4))
    except Exception as err:
        logging.warn(f'Error during writing to file occur: {err}')
    logging.info(f'New {len(offert_list)} offert added')
    logging.info('Scraping finished successfully!')

def connect_to_mongodb():
    # Load the configuration from the file
    config = load_config()

    # Extract the connection details from the configuration
    uri = config['uri']
    tls = config['tls']
    tlsCertificateKeyFile = config['tlsCertificateKeyFile']
    serverApiVersion = config['serverApiVersion']

    # Connect to MongoDB using the extracted details
    client = MongoClient(uri,
                         tls=tls,
                         tlsCertificateKeyFile=tlsCertificateKeyFile,
                         server_api=ServerApi(serverApiVersion))

    db = client[config['db']]
    collection = db[config['collection']]
    return collection

def load_config():
    # Load the configuration from the YAML file
    with open('/opt/airflow/config/mongodb_connection_config.yaml') as file:
        config = yaml.safe_load(file)
    return config

def read_json_from_file(filepath):
    with open(filepath) as file:
        data = json.load(file)
    return data

def save_data_to_mongodb(**kwargs):
    collection = connect_to_mongodb()
    filename = kwargs['ti'].xcom_pull(task_ids='save_data_to_json_format', key='filename')
    data = read_json_from_file(filename)
    collection.insert_many(data)

default_args = {
    'owner': 'slawomirse',
    'start_date': datetime(2023, 11, 11, 2),
    'retries': 5,
    'retry_delay': timedelta(minutes=5),
}

dag = DAG(
    'extract_data_from_otodom.pl_and_save_into_json_format',
    default_args=default_args,
    schedule_interval='0 0 10 * *',  # Run day 10th of the month
)


get_data = PythonOperator(
    task_id='collect_data',
    python_callable=get_content,
    dag=dag,
)

save_data = PythonOperator(
    task_id='save_data_to_json_format',
    python_callable=write_data_to_json_format,
    provide_context=True,
    dag=dag,
)

save_data_to_database = PythonOperator(
    task_id='save_data_to_mongodb',
    python_callable=save_data_to_mongodb,
    provide_context=True,
    dag=dag,
)

get_data >> save_data >> save_data_to_database