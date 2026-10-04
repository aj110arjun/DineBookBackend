import cloudinary
import cloudinary.uploader

from app.core.config import settings


cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
)


def upload_file(file, folder: str, resource_type: str = "auto"):
    result = cloudinary.uploader.upload(
        file,
        folder=folder,
        resource_type=resource_type,
    )
    return result


def delete_file(public_id: str, resource_type: str = "image"):
    return cloudinary.uploader.destroy(
        public_id,
        resource_type=resource_type,
    )
