from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from models import User, Upload

async def get_user_by_api_key(db: AsyncSession, api_key: str) -> User | None:
    result = await db.execute(
        select(User).where(User.api_key == api_key)
    )
    return result.scalars().first()

async def create_user(db: AsyncSession, name: str, api_key: str) -> User:
    user = User(name=name, api_key=api_key)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user

async def delete_user(db: AsyncSession, user_id: int) -> bool:
    user = await db.get(User, user_id)
    if not user:
        return False
    await db.delete(user)
    await db.commit()
    return True

async def list_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User))
    return result.scalars().all()

async def create_upload_record(db: AsyncSession, user_id: int, upload_id: str) -> Upload:
    upload = Upload(user_id=user_id, upload_id=upload_id)
    db.add(upload)
    await db.commit()
    await db.refresh(upload)
    return upload

async def get_upload_for_user(
    db: AsyncSession, user_id: int, upload_id: str | int
) -> Upload | None:
    """
    Если upload_id — не int, сразу подставляем в WHERE Upload.upload_id == upload_id,
    чтобы избежать ошибок при попытке db.get(Upload, 'string.wav').
    """
    upload = None
    # сначала — если передан целочисленный PK
    if isinstance(upload_id, int):
        upload = await db.get(Upload, upload_id)
    if not upload:
        result = await db.execute(
            select(Upload).where(
                Upload.user_id == user_id,
                Upload.upload_id == str(upload_id)
            )
        )
        upload = result.scalars().first()
    return upload

async def update_upload_status(db: AsyncSession, upload_id: str | int, status: str) -> None:
    upload = None
    if isinstance(upload_id, int):
        upload = await db.get(Upload, upload_id)
    if not upload:
        result = await db.execute(
            select(Upload).where(Upload.upload_id == str(upload_id))
        )
        upload = result.scalars().first()
    if upload:
        upload.status = status
        await db.commit()