import datetime
import uuid
from typing import Any, TypeVar, Generic, overload

from sqlalchemy import JSON, UUID, DateTime, ForeignKey, Integer, BigInteger, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, declarative_base

Base = declarative_base()

class NotificationMixin(Base):
    __tablename__ = "notifications"
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
UserPrimaryKeyType = TypeVar('UserPrimaryKeyType', type[Integer], type[BigInteger], type[String], type[UUID])
UserMappedPrimaryKeyType = TypeVar('UserMappedPrimaryKeyType')


class UserIdTypeMappingMetaClass(type):
    @overload
    def __getitem__(cls, user_id_type: type[Integer]) -> type[int]: ...
    @overload
    def __getitem__(cls, user_id_type: type[BigInteger]) -> type[int]: ...
    @overload
    def __getitem__(cls, user_id_type: type[String]) -> type[str]: ...
    @overload
    def __getitem__(cls, user_id_type: type[UUID]) -> type[uuid.UUID]: ...
    def __getitem__(cls, user_id_type: type) -> type:
        types_mapping = {
            String: str,
            Integer: int,
            BigInteger: int,
            UUID: uuid.UUID,
        }
        return types_mapping.get(user_id_type, int)
    
class UserIdTypeMapping(Generic[UserPrimaryKeyType], metaclass=UserIdTypeMappingMetaClass):
    pass


class GenericNotification(NotificationMixin, Generic[UserType, UserMappedPrimaryKeyType]):
    def get_user(self) -> UserType:
        raise NotImplementedError

    def get_user_id(self) -> UserPrimaryKeyType:
        raise NotImplementedError

    def set_user_id(self, user_id: UserPrimaryKeyType):
        raise NotImplementedError


def create_notification_model(
    user_model: type[UserType], 
    user_primary_key_field_name: str,
    user_primary_key_field_type: UserPrimaryKeyType
) -> type[GenericNotification[UserType, UserIdTypeMapping[UserPrimaryKeyType]]]:
    class Notification(NotificationMixin):
        __tablename__ = "notifications"
        __table_args__ = {"extend_existing": True}
        user_id: Mapped[UserIdTypeMapping[UserPrimaryKeyType]] = mapped_column(
            ForeignKey(getattr(user_model, user_primary_key_field_name)),
        )
        user: Mapped[UserType] = relationship(user_model, back_populates="notifications")

        def get_user(self) -> UserType:
            return self.user

        def get_user_id(self):
            return self.user_id

        @staticmethod
        def get_user_id_attr_name() -> str:
            return "user_id"

        @staticmethod
        def get_user_attr_name() -> str:
            return "user"

        def set_user_id(self, user_id: UserIdTypeMapping[UserPrimaryKeyType]):
            self.user_id = user_id

    user_model.notifications = relationship(
        Notification,
        order_by=Notification.created,
        back_populates="user",
    )
    return Notification
