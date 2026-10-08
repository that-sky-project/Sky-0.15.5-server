import pymysql
c=pymysql.connect(host='host.docker.internal',port=3306,user='wbsky',password='SkyServer2026!',database='wbsky')
cur=c.cursor()
cur.execute('SHOW TABLES')
print('tables:', [r[0] for r in cur.fetchall()])
cur.execute('SHOW CREATE TABLE chat_messages')
row=cur.fetchone()
print(row[1][:500] if row else 'NO chat_messages table')