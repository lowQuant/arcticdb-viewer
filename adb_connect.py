import os
import arcticdb as adb
import dotenv
dotenv.load_dotenv()

bucket_name = os.getenv("BUCKET_NAME")
aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
aws_region = os.getenv("AWS_REGION")

ac = adb.Arctic(f's3://s3.{aws_region}.amazonaws.com:{bucket_name}?region={aws_region}&access={aws_access_key_id}&secret={aws_secret_access_key}')


print(ac.list_libraries())