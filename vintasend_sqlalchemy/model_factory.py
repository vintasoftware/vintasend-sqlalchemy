import datetime
import uuid
from typing import Any, Generic, TypeVar

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, null
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.orm.decl_api import DeclarativeAttributeIntercept


class Base(DeclarativeBase):
    pass
    

class NotificationMixin(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True)  # noqa: A003
    notification_type: Mapped[str] = mapped_column("notification_type", String(50), nullable=False)
    title: Mapped[str] = mapped_column("title", String(255), nullable=False)
    status: Mapped[str] = mapped_column("status", String(50), nullable=False, default="PENDING_SEND")
    body_template: Mapped[str] = mapped_column("body_template", String(255), nullable=False)
    created: Mapped[datetime.datetime] = mapped_column(
        "created", DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated: Mapped[datetime.datetime] = mapped_column(
        "updated",
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    # Email specific fields
    subject_template: Mapped[str] = mapped_column("subject_template", String(255), nullable=True, default="")
    preheader_template: Mapped[str] = mapped_column("preheader_template", String(255), nullable=True, default="")
    context_name: Mapped[str] = mapped_column("context_name", String(255), nullable=True, default="")
    context_kwargs: Mapped[dict] = mapped_column("context_kwargs", JSON, default=dict)
    adapter_used: Mapped[str] = mapped_column("adapter_used", String(255), nullable=True)
    context_used: Mapped[dict | None] = mapped_column("context_used", JSON, nullable=True)
    adapter_extra_parameters: Mapped[dict | None] = mapped_column("adapter_extra_parameters", JSON, nullable=True)

    send_after = mapped_column("send_after", DateTime, nullable=True)

    def __str__(self):
        return f"{self.get_user()} - {self.notification_type} - {self.title} - {self.status}{f' (scheduled to {self.send_after})' if self.send_after else ''}"

    def get_user(self) -> Any:
        raise NotImplementedError

    def get_user_id(self) -> Any:
        raise NotImplementedError
    
    def get_user_email(self) -> str:
        raise NotImplementedError

    @staticmethod
    def get_user_id_attr_name() -> str:
        raise NotImplementedError

    @staticmethod
    def get_user_attr_name() -> str:
        raise NotImplementedError

    def set_user_id(self, user_id: Any):
        raise NotImplementedError


UserType = TypeVar('UserType', bound=DeclarativeBase)
UserPrimaryKeyType = TypeVar('UserPrimaryKeyType', int, str, uuid.UUID)


class NotificationMeta(DeclarativeAttributeIntercept):
    def __new__(cls, name, bases, dct, user_model, user_primary_key_field_name, user_primary_key_field_type):
        if user_primary_key_field_type == int:
            dct['user_id'] = mapped_column(ForeignKey(getattr(user_model, user_primary_key_field_name)))
            dct['set_user_id'] = lambda self, user_id: setattr(self, 'user_id', user_id)
        elif user_primary_key_field_type == str:
            dct['user_id'] = mapped_column(ForeignKey(getattr(user_model, user_primary_key_field_name)))
            dct['set_user_id'] = lambda self, user_id: setattr(self, 'user_id', user_id)
        elif user_primary_key_field_type == uuid.UUID:
            dct['user_id'] = mapped_column(ForeignKey(getattr(user_model, user_primary_key_field_name)))
            dct['set_user_id'] = lambda self, user_id: setattr(self, 'user_id', user_id)
        
        dct['user'] = relationship(user_model, backref="notifications")
        dct['get_user_id'] = lambda self: self.user_id
        dct['get_user'] = lambda self: self.user
        dct['__tablename__'] = "notifications"
        dct['__tableargs__'] = {"extend_existing": True}
        
        return super().__new__(cls, name, bases, dct)


class GenericNotification(
    NotificationMixin, 
    Generic[UserType, UserPrimaryKeyType],     
):
    __abstract__ = True

    user: Mapped[UserType]
    user_id: Mapped[UserPrimaryKeyType]
    
    def get_user_id(self) -> UserPrimaryKeyType:
        raise NotImplementedError

    def set_user_id(self, user_id: UserPrimaryKeyType) -> None:
        raise NotImplementedError
    
    def get_user(self) -> UserType:
        raise NotImplementedError

    @staticmethod
    def get_user_id_attr_name() -> str:
        return "user_id"

    @staticmethod
    def get_user_attr_name() -> str:
        return "user"
