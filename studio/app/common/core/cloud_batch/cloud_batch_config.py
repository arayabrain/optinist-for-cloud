from pydantic import BaseSettings, Field

from studio.app.dir_path import DIRPATH


class BatchConfig(BaseSettings):
    USE_AWS_BATCH: bool = Field(default=False, env="USE_AWS_BATCH")
    AWS_BATCH_JOB_QUEUE: str = Field(
        default="subscr-optinist-batch-queue", env="AWS_BATCH_JOB_QUEUE"
    )
    AWS_BATCH_JOB_DEFINITION: str = Field(
        default="subscr-optinist-job-def", env="AWS_BATCH_JOB_DEFINITION"
    )
    AWS_BATCH_JOB_ROLE: str = Field(default="", env="AWS_BATCH_JOB_ROLE")
    AWS_BATCH_REGION: str = Field(default="ap-northeast-1", env="AWS_BATCH_REGION")
    AWS_BATCH_S3_BUCKET_NAME: str = Field(default="", env="AWS_BATCH_S3_BUCKET_NAME")
    AWS_DEFAULT_PROVIDER: str = Field(default="S3", env="AWS_DEFAULT_PROVIDER")

    class Config:
        env_file = f"{DIRPATH.CONFIG_DIR}/.env"
        env_file_encoding = "utf-8"


BATCH_CONFIG = BatchConfig()
