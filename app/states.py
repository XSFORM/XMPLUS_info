from __future__ import annotations

from aiogram.fsm.state import StatesGroup, State


class AddStates(StatesGroup):
    waiting_user_id = State()
    waiting_username = State()
    waiting_duedatetime = State()
    waiting_note = State()


class RenewStates(StatesGroup):
    waiting_userid = State()
    waiting_new_due = State()
    waiting_confirm = State()


class DeleteStates(StatesGroup):
    waiting_userid = State()
    waiting_confirm = State()


class EditStates(StatesGroup):
    waiting_search = State()
    waiting_pick = State()
    waiting_value = State()


class DealerAssignStates(StatesGroup):
    waiting_ids = State()
    waiting_pick = State()


class AddDealerStates(StatesGroup):
    waiting_code = State()
    waiting_title = State()
    waiting_chat_id = State()


class MsgDealerStates(StatesGroup):
    waiting_text = State()


class BroadcastStates(StatesGroup):
    waiting_text = State()


class DealerEditStates(StatesGroup):
    waiting_search = State()
    waiting_value = State()


class DealerOrderStates(StatesGroup):
    waiting_names = State()


class DealerRenewStates(StatesGroup):
    waiting_userid = State()
    waiting_comment = State()


class DealerPayStates(StatesGroup):
    waiting_amount = State()


class BalanceStates(StatesGroup):
    waiting_amount = State()
    waiting_comment = State()
    waiting_price = State()


class PayAdminStates(StatesGroup):
    waiting_requisites = State()  # legacy (старые реквизиты метода)
    waiting_method_name = State()
    waiting_method_rename = State()
    waiting_variant_name = State()
    waiting_variant_new_req = State()
    waiting_variant_requisites = State()
    waiting_variant_rename = State()


class AdminKeyToDealerStates(StatesGroup):
    waiting_userid = State()
    waiting_username = State()
    waiting_keycode = State()


class OrderFulfillStates(StatesGroup):
    waiting_user_id = State()
    waiting_username = State()
    waiting_due = State()
    waiting_key_code = State()
    waiting_confirm = State()


class BackupStates(StatesGroup):
    waiting_restore_file = State()
    waiting_restore_confirm = State()


class RouterAddStates(StatesGroup):
    waiting_client_name = State()
    waiting_due = State()
    waiting_note = State()


class RouterEditStates(StatesGroup):
    waiting_search = State()
    waiting_value = State()


class RouterRenewStates(StatesGroup):
    waiting_search = State()
    waiting_due = State()


class RouterDeleteStates(StatesGroup):
    waiting_search = State()


