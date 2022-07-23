import asyncio
import importlib
import json
import os
import sys
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from typing import Tuple, List

from sqlalchemy import select, update

from app.enums.OperationEnum import OperationType
from app.enums.ProjectEnum import ProjectRoleEnum
from app.middleware.RedisManager import RedisHelper
from app.models import Base, async_session, DatabaseHelper, async_engine
from app.models.address import PityGateway
from app.models.basic import PityRelationField, init_relation
from app.models.environment import Environment
from app.models.gconfig import GConfig
from app.models.operation_log import PityOperationLog
from app.models.project import Project
from app.models.project_role import ProjectRole
from app.models.redis_config import PityRedis
from app.models.test_case import TestCase
from app.models.test_plan import PityTestPlan
from app.models.testcase_asserts import TestCaseAsserts
from app.models.user import User
from config import Config


class Mapper(object):
    log = None
    model = None

    @classmethod
    @RedisHelper.cache("dao")
    async def list_record(cls, condition=None, **kwargs):
        """
        通过查询条件获取数据，kwargs的key为参数名, value为参数值
        :param condition:
        :param kwargs:
        :return:
        """
        try:
            async with async_session() as session:
                sql = cls.query_wrapper(condition, **kwargs)
                result = await session.execute(sql)
                return result.scalars().all()
        except Exception as e:
            # 这边调用cls本身的log参数，写入日志+抛出异常
            cls.log.error(f"获取{cls.model}列表失败, error: {e}")
            raise Exception(f"获取数据失败")

    @classmethod
    @RedisHelper.cache("dao")
    async def list_record_with_pagination(cls, page, size, **kwargs):
        """
        通过分页获取数据
        :param page:
        :param size:
        :param kwargs:
        :return:
        """
        try:
            async with async_session() as session:
                sql = cls.query_wrapper(**kwargs)
                return await DatabaseHelper.pagination(page, size, session, sql)
        except Exception as e:
            cls.log.error(f"获取{cls.model}列表失败, error: {e}")
            raise Exception(f"获取数据失败")

    @classmethod
    def query_wrapper(cls, condition=None, **kwargs):
        conditions = condition if condition else list()
        if getattr(cls.model, "deleted_at", None):
            conditions.append(getattr(cls.model, "deleted_at") == 0)
        _sort = kwargs.get("_sort")
        if _sort is not None:
            # 需要去掉desc，不然会影响之前的sql执行
            kwargs.pop("_sort")
        # 遍历参数，当参数不为None的时候传递
        for k, v in kwargs.items():
            # 判断是否是like的情况
            like = isinstance(v, str) and (v.startswith("%") or v.endswith("%"))
            if like and len(v) == 2:
                continue
            # 如果是like模式，则使用Model.字段.like 否则用 Model.字段 等于
            DatabaseHelper.where(v, getattr(cls.model, k).like(v) if like else getattr(cls.model, k) == v,
                                 conditions)
        sql = select(cls.model).where(*conditions)
        if _sort and isinstance(_sort, tuple):
            for d in _sort:
                sql = getattr(sql, "order_by")(d)
        return sql

    @classmethod
    @RedisHelper.cache("dao")
    async def query_record(cls, session=None, **kwargs):
        try:
            if session:
                sql = cls.query_wrapper(**kwargs)
                result = await session.execute(sql)
                return result.scalars().first()
            async with async_session() as session:
                sql = cls.query_wrapper(**kwargs)
                result = await session.execute(sql)
                return result.scalars().first()
        except Exception as e:
            cls.log.error(f"查询{cls.model}失败, error: {e}")
            raise Exception(f"查询记录失败")

    @classmethod
    @RedisHelper.up_cache("dao")
    async def insert_record(cls, model, log=False, ss=None):
        try:
            if ss is None:
                async with async_session() as session:
                    async with session.begin():
                        session.add(model)
                        await session.flush()
                        session.expunge(model)
                    if log:
                        async with session.begin():
                            await asyncio.create_task(
                                cls.insert_log(session, model.create_user, OperationType.INSERT, model,
                                               key=model.id))
                    # 这里直接return了，不会继续走下面的add
                    return model
            ss.add(model)
            await ss.flush()
            ss.expunge(model)
            if log:
                await asyncio.create_task(
                    cls.insert_log(ss, model.create_user, OperationType.INSERT, model,
                                   key=model.id))
            return model
        except Exception as e:
            cls.log.error(f"添加{cls.model}记录失败, error: {e}")
            raise Exception(f"添加记录失败")

    @classmethod
    @RedisHelper.up_cache("dao")
    async def update_by_map(cls, user, *condition, **kwargs):
        try:
            async with async_session() as session:
                async with session.begin():
                    sql = update(cls.model).where(*condition).values(**kwargs, updated_at=datetime.now(),
                                                                     update_user=user)
                    await session.execute(sql)
        except Exception as e:
            cls.log.error(f"更新数据失败: {e}")
            raise Exception("更新数据失败")

    @classmethod
    @RedisHelper.up_cache("dao")
    async def update_record_by_id(cls, user: int, model, not_null=False, log=False):
        try:
            async with async_session() as session:
                async with session.begin():
                    query = cls.query_wrapper(id=model.id)
                    result = await session.execute(query)
                    now = result.scalars().first()
                    if now is None:
                        raise Exception("数据不存在")
                    old = deepcopy(now)
                    changed = DatabaseHelper.update_model(now, model, user, not_null)
                    await session.flush()
                    session.expunge_all()
                if log:
                    async with session.begin():
                        await asyncio.create_task(
                            cls.insert_log(session, user, OperationType.UPDATE, now, old, model.id,
                                           changed=changed))
                return now
        except Exception as e:
            cls.log.error(f"更新{cls.model}记录失败, error: {e}")
            raise Exception(f"更新数据失败")

    @classmethod
    async def _inner_delete(cls, session, user, value, log, key, exists):
        query = cls.query_wrapper(**{key: value})
        result = await session.execute(query)
        original = result.scalars().first()
        if original is None:
            if exists:
                raise Exception("记录不存在")
            return None
        DatabaseHelper.delete_model(original, user)
        await session.flush()
        session.expunge(original)
        if log:
            await asyncio.create_task(
                cls.insert_log(session, user, OperationType.DELETE, original, key=value))
            return original

    @classmethod
    @RedisHelper.up_cache("dao")
    async def delete_record_by_id(cls, session, user: int, value: int, log=True, key='id', exists=True,
                                  session_begin=False):
        """
        逻辑删除
        :param session_begin: 事务是否已经开始
        :param key:
        :param log:
        :param session: 默认的session，如果传入则使用传入的session
        :param user:
        :param value:
        :param exists: 是否一定需要记录存在，默认为True
        :return:
        """
        try:
            if session_begin:
                # 说明在外面已经开启了session
                return await cls._inner_delete(session, user, value, log, key, exists)
            async with session.begin():
                return await cls._inner_delete(session, user, value, log, key, exists)
        except Exception as e:
            cls.log.exception(f"删除{cls.model.__name__}记录失败: \n{e}")
            raise Exception(f"删除失败")

    @classmethod
    @RedisHelper.up_cache("dao")
    async def delete_records(cls, session, user, id_list: List[int], column="id", log=True):
        try:
            for id_ in id_list:
                query = cls.query_wrapper(**{column: id_})
                result = await session.execute(query)
                original = result.scalars().first()
                if original is None:
                    continue
                    # raise Exception("记录不存在")
                DatabaseHelper.delete_model(original, user)
                await session.flush()
                session.expunge(original)
                if log:
                    await asyncio.create_task(
                        cls.insert_log(session, user, OperationType.DELETE, original, key=id_))
        except Exception as e:
            cls.log.exception(f"删除{cls.model}记录失败, error: {e}")
            raise Exception(f"删除记录失败")

    @classmethod
    async def insert_log(cls, session, user, mode, now, old=None, key=None, changed=None):
        """
        根据relation插入日志
        :param changed:
        :param user:
        :param now:
        :param old:
        :param session:
        :param mode:
        :param key:
        :return:
        """
        diff, title = await cls.get_diff(session, mode, now, old, changed)
        tag = getattr(now, Config.TABLE_TAG, '未设置')
        diff_data = json.dumps(diff, ensure_ascii=False)
        model = PityOperationLog(user, mode, "&".join(title), tag, diff_data, key)
        session.add(model)

    @classmethod
    async def get_diff(cls, session, mode, now, old, changed):
        """
        根据新旧model获取2者的diff
        :param session:
        :param mode:
        :param now:
        :param old:
        :param changed:
        :return:
        """
        fields = getattr(now, Config.FIELD, None)
        # 根据要展示的字段数量(__show__)获取title数据
        fields_number = getattr(now, Config.SHOW_FIELD, 1)
        if fields:
            # 必须要展示至少1个字段
            fields = [f.name for f in fields[:fields_number]]
        else:
            fields = ['id']
        if not changed:
            if mode == OperationType.INSERT:
                changed_fields = await cls.get_fields(now)
            else:
                changed_fields = []
        else:
            changed_fields = changed
        detail_fields = [c for c in changed_fields if
                         c not in fields] if mode != OperationType.UPDATE else changed_fields
        result = []
        title = []
        for f in detail_fields:
            item = await cls.get_field_alias(session, getattr(now, Config.RELATION, None), f, now, old)
            result.append(item)
        for d in fields:
            item = await cls.get_field_alias(session, getattr(now, Config.RELATION, None), d, now, old)
            title.append(f"{item.get('name')}={item.get('now')}")
        return result, title

    @classmethod
    async def get_id_list(cls, ids):
        if ids == "":
            return []
        if isinstance(ids, int):
            # 说明是多个id
            id_list = [ids]
        else:
            id_list = list(map(int, ids.split(",")))
        return id_list

    @classmethod
    async def fetch_id_with_name(cls, session, id_field, name_field, old_id, new_id):
        """
        通用方法，通过id查询name等字段数据
        :param session:
        :param id_field:
        :param name_field:
        :param old_id:
        :param new_id:
        :return:
        """
        cls_ = id_field.parent.class_
        if old_id is None:
            id_list = await cls.get_id_list(new_id)
            data = await session.execute(select(cls_).where(getattr(cls_, id_field.name).in_(id_list)))
            result = data.scalars().all()
            if result is None:
                return new_id, None
            ans = []
            for r in result:
                ans.append(getattr(r, name_field.name, new_id))
            return ",".join(map(str, ans)), None
        new_list = await cls.get_id_list(new_id)
        old_list = await cls.get_id_list(old_id)
        id_list = old_list + new_list
        data = await session.execute(select(cls_).where(getattr(cls_, id_field.name).in_(id_list)))
        # old_value, new_value = old_id, new_id
        old_ans, new_ans = [], []
        mp = dict()
        for d in data.scalars():
            mp[getattr(d, id_field.name, None)] = getattr(d, name_field.name, None)
            # if getattr(d, id_field.name, None) == old_id:
            #     new_value = getattr(d, name_field.name, old_value)
            # else:
            #     old_value = getattr(d, name_field.name, new_value)
        for t in old_list:
            old_ans.append(mp.get(t, t))
        for i in new_list:
            new_ans.append(mp.get(i, i))
        return ",".join(map(str, new_ans)), ",".join(map(str, old_ans))

    @classmethod
    def get_json_field(cls, field):
        """
        遇到datetime等类型，进行转换
        :param field:
        :return:
        """
        if isinstance(field, datetime):
            return field.strftime("%Y-%m-%d %H:%M:%S")
        return field

    @classmethod
    async def get_field_alias(cls, session, relation: Tuple[PityRelationField], name, now, old=None):
        """
        获取别名操作，如果字段是别的表的主键，则还需要根据此字段查询别的表的对应字段
        :param session:
        :param relation: relation有2个值，第一个值是别的表对应的主键，第二个值是要显示的字段
        :param name:
        :param now:
        :param old:
        :return:
        """
        alias = getattr(now, Config.ALIAS, {})
        current_value = getattr(now, name, None)
        current_value = cls.get_json_field(current_value)
        old_value = getattr(old, name, None) if old is not None else None
        old_value = cls.get_json_field(old_value)
        if relation is not None:
            for r in relation:
                if r.field.name == name:
                    # 说明是id类型，需要转换为中文
                    if r.foreign is None:
                        return dict(name=alias.get(name, name), old=old_value, now=current_value)
                    if callable(r.foreign):
                        # foreign支持方法和数据库其他表，如果callable为True 说明是function
                        # 参考 ProjectRoleEnum.name方法 里面将int转为具体角色的方法
                        real_value = r.foreign(current_value)
                        real_old_value = r.foreign(old_value)
                        return dict(name=alias.get(name, name), old=real_old_value, now=real_value)
                    # 更新字段
                    id_field, name_field = r.foreign
                    current, old = await cls.fetch_id_with_name(session, id_field, name_field, old_value, current_value)
                    return dict(name=alias.get(name, name), old=old, now=current)
        return dict(name=alias.get(name, name), old=old_value, now=current_value)

    @classmethod
    async def get_fields(cls, model):
        """
        遍历字段，排除掉被忽略的字段
        :param model:
        :return:
        """
        ans = []
        fields = getattr(model, Config.FIELD, None)
        fields = [x.name for x in fields] if fields else list()
        for c in model.__table__.columns:
            if c.name in Config.IGNORE_FIELDS or (fields and c.name not in fields):
                continue
            ans.append(c.name)
        return ans

    @classmethod
    @RedisHelper.up_cache("dao")
    async def delete_by_id(cls, id):
        """
        物理删除
        :param id:
        :return:
        """
        try:
            async with async_session() as session:
                async with session.begin():
                    query = cls.query_wrapper(id=id)
                    result = await session.execute(query)
                    original = result.scalars().first()
                    if original is None:
                        raise Exception("记录不存在")
                    session.delete(original)
        except Exception as e:
            cls.log.error(f"逻辑删除{cls.model}记录失败, error: {e}")
            raise Exception(f"删除记录失败")


# from app.models.user import User
# from app.models.project import Project
# from app.models.project_role import ProjectRole
# from app.models.environment import Environment
# from app.models.constructor import Constructor
# from app.models.address import PityGateway
# from app.models.broadcast_read_user import PityBroadcastReadUser
# from app.models.testplan_follow_user import PityTestPlanFollowUserRel
# from app.models.database import PityDatabase
# from app.models.gconfig import GConfig
# from app.models.notification import PityNotification
# from app.models.operation_log import PityOperationLog
# from app.models.oss_file import PityOssFile
# from app.models.result import PityTestResult
# from app.models.test_case import TestCase
# from app.models.test_plan import PityTestPlan
# from app.models.testcase_asserts import TestCaseAsserts
# from app.models.testcase_data import PityTestcaseData
# from app.models.redis_config import PityRedis
# from app.models.testcase_directory import PityTestcaseDirectory
# from app.models.report import PityReport


def get_dao_path():
    """获取dao目录下所有的xxxDao.py"""
    dao_path_list = []
    for file in os.listdir(Config.DAO_PATH):
        # 拼接目录
        file_path = os.path.join(Config.DAO_PATH, file)
        # 判断过滤, 取有效目录
        if os.path.isdir(file_path) and '__pycache__' not in file:
            path_dict = defaultdict(list)
            # 获取目录下所有的xxxDao.py
            for py_file in os.listdir(file_path):
                if py_file.endswith('py') and 'init' not in py_file:
                    path_dict[file].append(py_file.split('.')[0])
            dao_path_list.append(path_dict)
    return dao_path_list


dao_path_list = get_dao_path()
for path in dao_path_list:
    for file_path, pys in path.items():
        # 拼接对应的dao目录
        son_dao_path = os.path.join(Config.DAO_PATH, file_path)
        # 导包时, 默认在这个路径下查找
        sys.path.append(son_dao_path)
        for py in pys:
            # 动态导包进去
            importlib.import_module(py)


async def create_table():
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# 设置项目角色映射关系
init_relation(ProjectRole, PityRelationField(ProjectRole.user_id, (User.id, User.name)),
              PityRelationField(ProjectRole.project_id, (Project.id, Project.name)),
              PityRelationField(ProjectRole.project_role, ProjectRoleEnum.name))

init_relation(PityRedis, PityRelationField(PityRedis.env, (Environment.id, Environment.name)))

init_relation(PityTestPlan, PityRelationField(PityTestPlan.env, (Environment.id, Environment.name)),
              PityRelationField(PityTestPlan.project_id, (Project.id, Project.name)),
              PityRelationField(PityTestPlan.msg_type, PityTestPlan.get_msg_type),
              PityRelationField(PityTestPlan.receiver, (User.id, User.name)))

init_relation(TestCase)

init_relation(TestCaseAsserts, PityRelationField(TestCaseAsserts.case_id, (TestCase.id, TestCase.name)))

init_relation(PityGateway, PityRelationField(PityGateway.env, (Environment.id, Environment.name)))

init_relation(GConfig, PityRelationField(GConfig.env, (Environment.id, Environment.name)))
