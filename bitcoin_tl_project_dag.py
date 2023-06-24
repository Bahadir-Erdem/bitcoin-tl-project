from datetime import timedelta, datetime
import pandas as pd
from pandas_gbq.exceptions import GenericGBQException
from google.cloud import bigquery
import json
import requests
from airflow import DAG
from airflow.utils.db import provide_session
from airflow.models import XCom
from airflow.operators.python import PythonOperator

DAG_ID = 'bitcoin_usd_to_tl_project'  # optional
USD_TO_TL_TABLE_ID = ''  # TODO: Set table_id to the ID of the table to create. Example: financebase.finance.usd_to_tl
USD_TO_TL_PROJECT_ID = USD_TO_TL_TABLE_ID.split('.', 1)[0]
BITCOIN_TL_TABLE_ID = ''  # TODO: Set table_id to the ID of the table to create. Example: financebase.finance.bitcoin_tl
EXCHANGERATE_API_KEY = ''  # TODO
COINRANKING_API_KEY = ''  # TODO


def get_usd_to_tl_timestamp(series):
    series = series.astype("datetime64[ns]")
    series = series.map(lambda x: round(datetime.timestamp(x)))
    return series.iloc[0]


def call_usd_to_tl_api():
    url = f'https://v6.exchangerate-api.com/v6/{EXCHANGERATE_API_KEY}/pair/USD/TRY/1'
    response = requests.get(url)
    tl_currency_raw_dict = response.json()
    return tl_currency_raw_dict


def insert_new_usd_to_tl_to_gbq(usd_to_tl_df):
    client = bigquery.Client()

    job_config = bigquery.LoadJobConfig(
        schema=[
            bigquery.SchemaField("fetched_datetime", bigquery.enums.SqlTypeNames.DATETIME),
            bigquery.SchemaField("usd_to_tl", bigquery.enums.SqlTypeNames.FLOAT64),
            bigquery.SchemaField("next_usd_to_tl_datetime", bigquery.enums.SqlTypeNames.DATETIME)
        ],

        write_disposition="WRITE_APPEND",
    )

    job = client.load_table_from_dataframe(
        usd_to_tl_df, USD_TO_TL_TABLE_ID, job_config=job_config
    )
    job.result()


def get_usd_to_tl():
    sql = f"""
            SELECT *
            FROM `{USD_TO_TL_TABLE_ID}`
            ORDER BY fetched_datetime DESC
            LIMIT 1
            """

    try:
        tl_currency_stored_df = pd.read_gbq(sql, project_id=USD_TO_TL_PROJECT_ID, dialect='standard')
        tl_currency_timestamp = get_usd_to_tl_timestamp(tl_currency_stored_df["next_usd_to_tl_datetime"])
        current_time_timestamp = round(datetime.timestamp(datetime.now()))
        if current_time_timestamp - tl_currency_timestamp >= 0:
            usd_to_tl_raw_dict = call_usd_to_tl_api()  # 55 içindeki if ve except içindeki kod refactor edilebilir belki
            usd_to_tl_df = clean_usd_to_tl(usd_to_tl_raw_dict)
            insert_new_usd_to_tl_to_gbq(usd_to_tl_df)
            return usd_to_tl_df
        else:
            return tl_currency_stored_df

    except GenericGBQException:
        usd_to_tl_raw_dict = call_usd_to_tl_api()
        usd_to_tl_df = clean_usd_to_tl(usd_to_tl_raw_dict)
        insert_new_usd_to_tl_to_gbq(usd_to_tl_df)
        return usd_to_tl_df


def get_bitcoin_usd():
    headers = {
        'x-access-token': COINRANKING_API_KEY
    }

    bitcoin_response = requests.request("GET", "https://api.coinranking.com/v2/coin/Qwsogvtv82FCd/price",
                                        headers=headers)
    bitcoin_dict = json.loads(bitcoin_response.text)
    return bitcoin_dict["data"]


def clean_usd_to_tl(usd_to_tl_raw_dict):
    keys = ["time_last_update_unix", "conversion_rate", "time_next_update_unix"]
    tl_currency_dict_cleaned = {key: usd_to_tl_raw_dict[key] for key in keys}
    tl_currency_df = pd.DataFrame.from_records(tl_currency_dict_cleaned, index=[0])
    tl_currency_df.rename({"time_last_update_unix": "fetched_datetime", "conversion_rate": "usd_to_tl",
                           "time_next_update_unix": "next_usd_to_tl_datetime"}, axis="columns", inplace=True)
    tl_currency_df = tl_currency_df[["fetched_datetime", "usd_to_tl", "next_usd_to_tl_datetime"]]
    tl_currency_df[["fetched_datetime", "next_usd_to_tl_datetime"]] = tl_currency_df[
        ["fetched_datetime", "next_usd_to_tl_datetime"]].applymap(lambda x: datetime.fromtimestamp(x))
    return tl_currency_df


def clean_bitcoin_usd(ti):
    bitcoin_dict = ti.xcom_pull(task_ids="get_bitcoin_usd")
    bitcoin_df = pd.DataFrame.from_records(bitcoin_dict, index=[0])
    bitcoin_df["price"] = bitcoin_df["price"].astype("float64")
    bitcoin_df = bitcoin_df[["timestamp", "price"]]
    bitcoin_df["timestamp"] = bitcoin_df["timestamp"].map(lambda x: datetime.fromtimestamp(x))
    bitcoin_df.rename({"timestamp": "datetime"}, axis="columns", inplace=True)
    return bitcoin_df


def convert_bitcoin_usd_to_tl(ti):
    tl_currency_df = ti.xcom_pull(task_ids="get_usd_to_tl")
    bitcoin_df = ti.xcom_pull(task_ids="clean_bitcoin_usd")
    bitcoin_df["price"] = bitcoin_df["price"] * tl_currency_df["usd_to_tl"]
    return bitcoin_df


def upload_to_gbq(ti):
    bitcoin_df = ti.xcom_pull(task_ids="convert_bitcoin_usd_to_tl")
    client = bigquery.Client()

    job_config = bigquery.LoadJobConfig(
        schema=[
            bigquery.SchemaField("datetime", bigquery.enums.SqlTypeNames.DATETIME),
            bigquery.SchemaField("price", bigquery.enums.SqlTypeNames.FLOAT64)
        ],
        write_disposition="WRITE_APPEND",
    )

    job = client.load_table_from_dataframe(
        bitcoin_df, BITCOIN_TL_TABLE_ID, job_config=job_config
    )
    job.result()


@provide_session
def cleanup_xcom(session=None):
    session.query(XCom).filter(XCom.dag_id == DAG_ID).delete()


default_args = {
    'owner': 'admin',
    'start_date': datetime(2022, 5, 17),
    'retries': 3,
    'retry_delay': timedelta(minutes=5),
}

with DAG(DAG_ID,
         default_args=default_args,
         description='Açiklama Yok',
         schedule='*/5 * * * *',
         catchup=False,
         tags=['bitcoin, usd, tl']) as dag:
    get_usd_to_tl_task = PythonOperator(task_id="get_usd_to_tl", python_callable=get_usd_to_tl)
    get_bitcoin_usd_task = PythonOperator(task_id="get_bitcoin_usd", python_callable=get_bitcoin_usd)
    clean_bitcoin_usd_task = PythonOperator(task_id="clean_bitcoin_usd", python_callable=clean_bitcoin_usd)
    convert_bitcoin_usd_to_tl_task = PythonOperator(task_id="convert_bitcoin_usd_to_tl",
                                                    python_callable=convert_bitcoin_usd_to_tl)
    upload_to_gbq_task = PythonOperator(task_id="pandas_to_gbq", python_callable=upload_to_gbq)
    cleanup_xcom_task = PythonOperator(task_id="cleanup_xcom", python_callable=cleanup_xcom)

    get_bitcoin_usd_task >> clean_bitcoin_usd_task
    [get_usd_to_tl_task, clean_bitcoin_usd_task] >> convert_bitcoin_usd_to_tl_task >> upload_to_gbq_task
    upload_to_gbq_task >> cleanup_xcom_task
