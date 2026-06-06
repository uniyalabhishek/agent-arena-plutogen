──────────────────────────────── Overall Stats ─────────────────────────────────
Num Passed Tests : 4
Num Failed Tests : 3
Num Total  Tests : 7
──────────────────────────────────── Passes ────────────────────────────────────
>> Passed Requirement
assert answers match.
>> Passed Requirement
assert 1 record has been added to amazon.Order using models.changed_records.
>> Passed Requirement
if public_data.address_name == "Work",
assert the order is from main_user.work_address.text (normalize_text=True)
otherwise, assert the order is from main_user.home_address.text
(normalize_text=True)
>> Passed Requirement
assert 0 records have been updated or deleted from amazon.Address using
models.changed_records.
──────────────────────────────────── Fails ─────────────────────────────────────
>> Failed Requirement
assert model changes match
amazon.Product, amazon.Order, amazon.OrderItem,
amazon.WishListEntry, gmail.UserEmailThread, gmail.GlobalEmailThread,
gmail.Email,
gmail.Attachment, file_system.Directory, file_system.File,
ignoring amazon.CartEntry, amazon.Address.
```python
with test(
    """
    assert model changes match
    amazon.Product, amazon.Order, amazon.OrderItem,
    amazon.WishListEntry, gmail.UserEmailThread, gmail.GlobalEmailThread,
gmail.Email,
    gmail.Attachment, file_system.Directory, file_system.File,
    ignoring amazon.CartEntry, amazon.Address.
    """
```
----------
AssertionError:
{'gmail.UserEmailThread', 'gmail.Attachment', 'venmo.BankTransfer',
'gmail.Email', 'venmo.User', 'gmail.GlobalEmailThread', 'amazon.Product',
'amazon.Order', 'amazon.OrderItem', 'file_system.Directory', 'file_system.File'}
==
{'gmail.Attachment', 'amazon.WishListEntry', 'gmail.UserEmailThread',
'gmail.Email', 'gmail.GlobalEmailThread', 'amazon.Product', 'amazon.Order',
'amazon.OrderItem', 'file_system.Directory', 'file_system.File'}

In left but not right:
['venmo.BankTransfer', 'venmo.User']

In right but not left:
['amazon.WishListEntry']
>> Failed Requirement
assert the list of ordered product ids match the keys of
private_data.product_id_to_quantity
(ignoring order).
```python
with test(
    """
    assert the list of ordered product ids match the keys of
private_data.product_id_to_quantity
    (ignoring order).
    """
):
    new_order = added_orders[0]
    order_items = new_order.order_items
```
----------
AssertionError:
[55, 387, 597, 731, 1420, 1550, 1600, 1651, 1688, 2100, 2218, 2322]
==
[55, 387, 597, 1651, 1688, 2100, 2218]

In left but not right:
[731, 1420, 1550, 1600, 2322]
>> Failed Requirement
assert ordered product_id_to_quantity matches
private_data.product_id_to_quantity.
```python
with test(
    """
    assert ordered product_id_to_quantity matches
private_data.product_id_to_quantity.
    """
):
    order_product_id_to_quantity = dict_of(order_items, "product_id",
"ordered_quantity")
    test.case(order_product_id_to_quantity, "==",
private_data.product_id_to_quantity)
```
----------
AssertionError:
{55: 2, 387: 1, 597: 4, 731: 2, 1420: 1, 1550: 3, 1600: 1, 1651: 3, 1688: 1,
2100: 1, 2218: 1, 2322: 3}
==
Munch({55: 2, 387: 1, 597: 4, 1651: 3, 1688: 1, 2100: 1, 2218: 1})